"""Durable single-delivery queue for routed results sent to owner DMs.

A network send and SQLite cannot participate in one atomic transaction.
This intentionally guarantees AT-MOST-ONCE dispatch even on uncertain
timeouts: claim before send, never automatically re-send a claimed item.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import time

import agent_channel_routing as routing

log = logging.getLogger(__name__)


@contextmanager
def _db(settings):
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_channel_result_outbox(
            task_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            operator TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            text TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            claimed_at REAL,
            completed_at REAL,
            send_id TEXT)""")
        with conn:
            yield conn
    finally:
        conn.close()


def enqueue(settings, *, task_id, channel, operator, chat_id, text):
    """Called only by a completed, approved router result bridge."""
    if not routing.enabled(settings) or channel not in ("telegram", "feishu"):
        return False
    if (not task_id or not chat_id or not isinstance(text, str)
            or not text.strip() or operator != routing._owner(settings, channel)):
        return False
    with _db(settings) as conn:
        row = conn.execute(
            "SELECT channel,operator,chat_id FROM agent_channel_result_outbox "
            "WHERE task_id=?", (task_id,)).fetchone()
        if row:
            if (row["channel"], row["operator"], row["chat_id"]) != (channel, operator, chat_id):
                raise ValueError("routed result ID collision")
            return False
        conn.execute(
            """INSERT INTO agent_channel_result_outbox
               (task_id,channel,operator,chat_id,text)
               VALUES(?,?,?,?,?)""",
            (task_id, channel, operator, chat_id, text))
    return True


def _claim(settings, channel):
    """One process atomically claims; a restart never repeats an uncertain send."""
    if not routing.enabled(settings):
        return None
    with _db(settings) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM agent_channel_result_outbox
               WHERE channel=? AND state='pending'
               ORDER BY rowid LIMIT 1""", (channel,)).fetchone()
        if row is None:
            return None
        # Reverify current allowlist before dequeuing (revocation fail-closed).
        if row["operator"] != routing._owner(settings, channel):
            db.execute("UPDATE agent_channel_result_outbox SET state='blocked' WHERE task_id=?",
                       (row["task_id"],))
            return None
        db.execute(
            """UPDATE agent_channel_result_outbox
               SET state='claimed',claimed_at=? WHERE task_id=? AND state='pending'""",
            (time.time(), row["task_id"]))
        return dict(row)


def _finish(settings, task_id, status, message_id=None):
    with _db(settings) as db:
        db.execute(
            """UPDATE agent_channel_result_outbox
               SET state=?,send_id=?,completed_at=?
               WHERE task_id=? AND state='claimed'""",
            (status, str(message_id or "")[:100], time.time(), task_id))


async def consume_once(settings, channel, send):
    """Send one routed result; new rows use retryable acknowledged outbox.

    Legacy pre-migration rows keep their existing at-most-once semantics.
    """
    import agent_result_outbox
    result = await agent_result_outbox.consume_once(settings, channel, send)
    if result is not None:
        return result
    row = _claim(settings, channel)
    if row is None:
        return 0
    try:
        # Exfiltration policy wins over convenience, even for completed tasks.
        import egress_taint
        import agents
        if (egress_taint.requires_approval(
                settings, "agent.reply", operator_id=row["operator"],
                channel=channel, chat_id=row["chat_id"])
            or egress_taint.requires_approval(
                settings, "agent.reply", operator_id=agents.WEB_OPERATOR,
                channel="web", chat_id=agents.chat_id_for("default"))):
            _finish(settings, row["task_id"], "blocked")
            return 0
        message_id = await asyncio.wait_for(
            send(row["chat_id"], row["text"]), timeout=15)
        # Existing Feishu plain-text sender may return None on a successful
        # send. A network exception is unknown, and we do not retry it.
        _finish(settings, row["task_id"], "sent", message_id)
        return 1
    except Exception:
        log.exception("Agent result send uncertain; avoiding duplicate delivery")
        _finish(settings, row["task_id"], "unknown")
        return 0


async def run(settings, channel, send):
    import agent_result_outbox
    import agent_security
    last_purge = 0.0
    while routing.enabled(settings) or agent_result_outbox.configured(settings):
        try:
            if agent_security.killed(settings):
                await asyncio.sleep(1)
                continue  # Do not exit: unkill resumes without bot restart.
            if time.time() - last_purge > 3600:
                agent_result_outbox.purge(settings)
                last_purge = time.time()
            await consume_once(settings, channel, send)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Agent result outbox poll failed")
        await asyncio.sleep(1)
