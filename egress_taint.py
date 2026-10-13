"""Small, durable session-taint marker for private-data reads (opt in with egress)."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Any


def enabled(settings: Any = None) -> bool:
    from egress_proxy import enabled as proxy_enabled
    # Background children must share durable parent taint even when the optional
    # egress proxy itself is not installed/enabled.
    return proxy_enabled(settings) or bool(getattr(settings, "background_subagents_enabled", False))


def _db(settings: Any) -> sqlite3.Connection:
    path = Path(settings.codex_memory_root) / "egress_taint.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=3)
    os.chmod(path, 0o600)
    db.execute("PRAGMA busy_timeout=3000")
    db.execute("""CREATE TABLE IF NOT EXISTS private_reads (
        operator TEXT NOT NULL, channel TEXT NOT NULL, session TEXT NOT NULL,
        PRIMARY KEY(operator, channel, session)
    )""")
    return db


def private_read(tool: str) -> bool:
    """Conservatively mark attempts to read personal data, even on tool failure."""
    name = tool.lower()
    return (name.startswith(("memory.", "gmail.", "inbox.", "secrets.", "secret."))
            or name in ("email.inbox", "email.read", "email.search",
                        "email.recent", "file.search_private",
                        "calendar.search", "contacts.search", "notes.search",
                        "files.search"))


def outbound(settings: Any, tool: str) -> bool:
    """Reuse the existing draft-card outbound classification."""
    from outbound_draft_cards import is_outbound
    name = tool.lower()
    network_read = (name in ("web.fetch", "web.text", "web.headers", "web.search")
                    or name.startswith(("browser.", "research."))
                    or (getattr(settings, "background_subagents_enabled", False)
                        and name.startswith("mcp.")))
    return is_outbound(settings, tool) or network_read


def mark_read(settings: Any, tool: str, *, operator_id: str = "",
              channel: str = "", chat_id: str = "") -> None:
    child_private = (getattr(settings, "background_subagents_enabled", False)
                     and tool.lower().startswith(("notes.", "calendar.", "contacts.",
                                                  "files.", "file.", "project.", "mcp.")))
    if not (enabled(settings) and (private_read(tool) or child_private) and chat_id and channel):
        return
    with _db(settings) as db:
        db.execute("INSERT OR IGNORE INTO private_reads VALUES (?,?,?)",
                   (operator_id or "", channel, chat_id))


def is_tainted(settings: Any, *, operator_id: str = "",
               channel: str = "", chat_id: str = "") -> bool:
    if not (enabled(settings) and channel and chat_id):
        return False
    with _db(settings) as db:
        row = db.execute("SELECT 1 FROM private_reads WHERE operator=? AND channel=? AND session=?",
                         (operator_id or "", channel, chat_id)).fetchone()
        return row is not None


def requires_approval(settings: Any, tool: str, *, operator_id: str = "",
                      channel: str = "", chat_id: str = "") -> bool:
    return outbound(settings, tool) and is_tainted(
        settings, operator_id=operator_id, channel=channel, chat_id=chat_id)


def clear_session(settings: Any, *, operator_id: str = "",
                  channel: str = "", chat_id: str = "") -> None:
    """Explicit reset only; do not clear a taint when a new chat turn starts."""
    if not (enabled(settings) and channel and chat_id):
        return
    with _db(settings) as db:
        db.execute("DELETE FROM private_reads WHERE operator=? AND channel=? AND session=?",
                   (operator_id or "", channel, chat_id))
