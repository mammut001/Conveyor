"""Durable origin mapping and owner-visible, untrusted routed results.

Routing metadata deliberately omits the owner's original message body.
Only the approved task's opaque key joins the mailbox with its origin.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import logging
import re
import sqlite3

log = logging.getLogger(__name__)
_KEY = re.compile(r"^delegate-[a-zA-Z0-9._:-]{1,120}$")
_SCHEMA = """CREATE TABLE IF NOT EXISTS main_router_routes (
    approval_key TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    origin_channel TEXT NOT NULL,
    origin_operator TEXT NOT NULL,
    origin_chat TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
)"""


@contextmanager
def _connect(settings):
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute(_SCHEMA)
        with db:
            yield db
    finally:
        db.close()


def register(settings, *, key, agent_id, agent_name, channel, chat_id, operator):
    import main_router
    if not main_router.enabled(settings) or not _KEY.fullmatch(key):
        raise ValueError("router disabled or invalid key")
    if channel not in ("web", "telegram", "feishu") or not all(
            isinstance(v, str) and v for v in (agent_id, agent_name, chat_id, operator)):
        raise ValueError("invalid owner")
    with _connect(settings) as db:
        row = db.execute("SELECT * FROM main_router_routes WHERE approval_key=?", (key,)).fetchone()
        identity = (agent_id, agent_name, channel, operator, chat_id)
        if row is not None:
            if tuple(row[c] for c in (
                    "agent_id", "agent_name", "origin_channel",
                    "origin_operator", "origin_chat")) != identity:
                raise ValueError("approval key already assigned elsewhere")
            return
        db.execute("""INSERT INTO main_router_routes
                  (approval_key,agent_id,agent_name,origin_channel,
                   origin_operator,origin_chat) VALUES (?,?,?,?,?,?)""",
                   (key, *identity))


def remove(settings, key):
    with _connect(settings) as db:
        db.execute("DELETE FROM main_router_routes WHERE approval_key=? AND delivered=0", (key,))


def deliver(settings, task, text):
    """Return child result to original owner, one delivery per task.

    This is the owner's durable session/event feed; it does not impersonate
    a user or write a system-level instruction. No raw request in route log.
    """
    import main_router
    if not main_router.enabled(settings):
        return False
    key = task.get("idempotency_key", "")
    if not isinstance(key, str) or not key.startswith("delegate-"):
        return False
    with _connect(settings) as db:
        row = db.execute("SELECT * FROM main_router_routes WHERE approval_key=?", (key,)).fetchone()
        if row is None or row["agent_id"] != task["to_agent"]:
            return False
        # Claim delivery under SQLite's exclusive write lock, before the
        # external transcript write; conservative at-most-once delivery.
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute("UPDATE main_router_routes SET delivered=1 "
                             "WHERE approval_key=? AND delivered=0", (key,))
        if claimed.rowcount == 0:
            return False
        route = dict(row)
    from transcript_store import get_transcript_store, session_identity
    from redaction import redact_text
    from html import escape
    session = session_identity(route["origin_channel"], route["origin_chat"],
                               route["origin_operator"])
    name = route["agent_name"]
    # Body is escaped and wrapped as untrusted DATA. It grants no approval.
    body = escape(redact_text(str(text or ""))[:2400], quote=True)
    output = (f"[{name}] delegated result (untrusted data):\n"
              f'<agent-result untrusted="true" instruction_authority="false" '
              f'approval_authority="false">\n{body}\n</agent-result>')
    get_transcript_store(settings).append(
        session, "assistant", output, kind="main_router_result",
        channel=route["origin_channel"], operator_id=route["origin_operator"],
        source_chat_id=route["origin_chat"],
        metadata={"agent_id": route["agent_id"], "task_id": task["id"],
                  "untrusted": True, "approval_authority": False})
    try:
        from agent_events import emit_event
        emit_event(settings, "assistant.completed", task["id"],
                   {"text": output, "agent_id": route["agent_id"]},
                   session_id=route["origin_chat"])
    except Exception:
        log.exception("Unable to signal routed result event")
    return True
