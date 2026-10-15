"""Multi-Agent security primitives: hot kill, atomic admission, redacted audit, TTL.

No message bodies, arguments, chat IDs, or secrets ever enter audit rows.
Queue admission is called within the mailbox BEGIN IMMEDIATE transaction.
"""
from __future__ import annotations
import asyncio
import os
import re
import sqlite3
import time
from pathlib import Path

_AGENT = re.compile(r"^[a-z0-9]{1,32}$")
_EVENTS = frozenset({"route", "delegate", "bind", "cancel", "deny"})
_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_security_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    happened_at REAL NOT NULL,
    operator TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('route','delegate','bind','cancel','deny')),
    agent_id TEXT NOT NULL,
    reason TEXT NOT NULL
)"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_agent_security_audit_scope ON agent_security_audit(operator,agent_id,happened_at)"


def killed() -> bool:
    return os.environ.get("CONVEYOR_AGENTS_KILL", "").lower() in ("1", "true", "yes", "on")


def _setting(name, default, lo, hi):
    try:
        val = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, val))


def delegation_configured(settings):
    # Ignore the kill switch solely for authenticated emergency cancellation.
    import agent_context
    return (agent_context.enabled(settings)
            and os.environ.get("CONVEYOR_AGENT_MAILBOX", "").lower()
                in ("1", "true", "yes", "on")
            and os.environ.get("CONVEYOR_AGENT_DELEGATION", "").lower()
                in ("1", "true", "yes", "on"))


def rate_limit():
    return _setting("CONVEYOR_AGENT_RATE_PER_HOUR", 60, 1, 10000)


def queue_cap():
    return _setting("CONVEYOR_AGENT_MAX_PENDING", 100, 1, 100000)


def retention_days():
    return _setting("CONVEYOR_AGENT_RETENTION_DAYS", 14, 1, 365)


def ensure(conn):
    conn.execute(_SCHEMA)
    conn.execute(_INDEX)


def _reason(value):
    # Fixed class names only, not freeform payloads or user text.
    allowed = frozenset((
        "explicit", "persona", "model", "main", "opt_out", "no_candidate",
        "unknown_selection", "explicit_denied", "untrusted_owner",
        "identity_denied", "selected_conversation", "ambiguous",
        "approved", "queued", "rate_limited", "queue_full", "killed",
        "owner_selected", "owner_rejected", "task_cancelled",
        "operator_stop", "task_stop", "revoked", "disabled", "legacy_queued",
    ))
    return value if value in allowed else "denied"


def audit_tx(conn, *, operator, event, agent_id, reason, now=None):
    if event not in _EVENTS or not isinstance(agent_id, str) or not _AGENT.fullmatch(agent_id):
        return
    ensure(conn)
    conn.execute(
        "INSERT INTO agent_security_audit(happened_at,operator,event,agent_id,reason) VALUES (?,?,?,?,?)",
        (time.time() if now is None else now, str(operator)[:100],
         event, agent_id, _reason(reason)))


def audit(settings, *, operator, event, agent_id, reason):
    if not operator or not isinstance(agent_id, str) or not _AGENT.fullmatch(agent_id):
        return
    # Separate connection: callers MUST NOT invoke this inside another
    # mailbox transaction; for those paths call audit_tx(conn).
    from agent_mailbox_db import transaction
    with transaction(settings) as conn:
        audit_tx(conn, operator=operator, event=event,
                 agent_id=agent_id, reason=reason)


def admit(conn, *, operator, to_agent, kind, now):
    """Atomic admission; caller checked idempotency before invoking."""
    if killed():
        audit_tx(conn, operator=operator, event="deny", agent_id=to_agent,
                 reason="killed", now=now)
        raise ValueError("agents disabled by emergency kill switch")
    if kind == "result":
        # Internal output is not a new owner request but must not bypass
        # the recipient's mailbox pending cap.
        check_capacity(conn, operator=operator, to_agent=to_agent)
        return
    if kind not in ("message", "task", "task_request", "task_result"):
        return
    ensure(conn)
    count = conn.execute(
        """SELECT COUNT(*) FROM agent_mailbox
           WHERE operator=? AND created_at>=? AND created_at<=?
           AND kind IN ('message','task','task_request','task_result')""",
        (operator, now-3600, now)).fetchone()[0]
    count += conn.execute(
        """SELECT COUNT(*) FROM agent_security_audit
           WHERE operator=? AND event='delegate'
           AND reason='legacy_queued' AND happened_at>=?""",
        (operator, now-3600)).fetchone()[0]
    if count >= rate_limit():
        audit_tx(conn, operator=operator, event="deny", agent_id=to_agent,
                 reason="rate_limited", now=now)
        raise ValueError("per-operator agent rate limit exceeded")
    check_capacity(conn, operator=operator, to_agent=to_agent)


def check_capacity(conn, *, operator, to_agent):
    """Also used for worker-written result rows (without charging owner rate)."""
    pending = conn.execute(
        """SELECT COUNT(*) FROM agent_mailbox
           WHERE operator=? AND to_agent=?
           AND status IN ('queued','leased','failed')""",
        (operator, to_agent)).fetchone()[0]
    if pending >= queue_cap():
        raise ValueError("agent mailbox queue full")


def admit_legacy_message(settings, *, operator, to_agent):
    """Serialized rate admission for pre-Mailbox named-agent transcript sends.

    Legacy writes are not mailbox enqueues, but still consume the operator's
    Agent messaging budget. No payload enters the security audit.
    """
    if killed():
        raise ValueError("agents disabled by emergency kill switch")
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("BEGIN IMMEDIATE")
        ensure(conn)
        now = time.time()
        used = conn.execute(
            """SELECT COUNT(*) FROM agent_security_audit WHERE operator=?
               AND event='delegate' AND reason='legacy_queued'
               AND happened_at>=?""", (operator, now-3600)).fetchone()[0]
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='agent_mailbox'").fetchone():
            used += conn.execute(
                """SELECT COUNT(*) FROM agent_mailbox WHERE operator=?
                   AND created_at>=? AND kind IN ('message','task','task_request','task_result')""",
                (operator, now-3600)).fetchone()[0]
        if used >= rate_limit():
            raise ValueError("per-operator agent rate limit exceeded")
        audit_tx(conn, operator=operator, event="delegate",
                 agent_id=to_agent, reason="legacy_queued", now=now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit_for_agent(settings, agent_id, operator, limit=50):
    from agent_mailbox_db import transaction
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid audit limit")
    with transaction(settings) as conn:
        ensure(conn)
        rows = conn.execute(
            """SELECT happened_at,event,agent_id,reason FROM agent_security_audit
               WHERE operator=? AND agent_id=?
               ORDER BY happened_at DESC,id DESC LIMIT ?""",
            (operator, agent_id, limit)).fetchall()
        return [dict(row) for row in rows]


def purge_once(settings, *, now=None, batch=100):
    """Bounded, FK-safe deletion: descendants before ancestors, at most batch."""
    from agent_mailbox_db import transaction
    if type(batch) is not int or not 1 <= batch <= 500:
        raise ValueError("invalid purge batch")
    cutoff = (time.time() if now is None else now) - retention_days()*86400
    with transaction(settings) as conn:
        total = 0
        # Children must be removed before their parent because FK=ON.
        # Do not purge live parent jobs with pending child results.
        for condition in (
            "kind IN ('result','task_result')",
            "status IN ('done','dead')",
        ):
            count = batch - total
            if not count:
                break
            cursor = conn.execute(
                """DELETE FROM agent_mailbox WHERE id IN
                   (SELECT a.id FROM agent_mailbox a
                    WHERE a.created_at < ? AND """ + condition + """
                      AND NOT EXISTS
                        (SELECT 1 FROM agent_mailbox c
                         WHERE c.parent_message_id=a.id)
                    ORDER BY a.created_at,a.id LIMIT ?)""",
                (cutoff, count))
            total += cursor.rowcount
        # Bound audit retention as well, without touching recent rows.
        conn.execute(
            """DELETE FROM agent_security_audit WHERE id IN
               (SELECT id FROM agent_security_audit WHERE happened_at<?
                ORDER BY happened_at,id LIMIT ?)""",
            (cutoff, batch)) if _audit_exists(conn) else None
        return total


def _audit_exists(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='agent_security_audit'"
    ).fetchone() is not None


async def retention_worker(settings):
    import agent_mailbox
    while agent_mailbox.enabled(settings) or killed():
        try:
            purge_once(settings, batch=100)
        except sqlite3.Error:
            import logging
            logging.getLogger(__name__).exception("Agent mailbox retention failed")
        await asyncio.sleep(3600)


def start(loop, settings):
    import agent_mailbox
    if not agent_mailbox.enabled(settings):
        return None
    return loop.create_task(retention_worker(settings), name="conveyor-agent-retention")
