"""Transactional, acknowledged outbound notification queue for routed results.

One durable row per committed result and destination. Network acknowledgement
cannot be atomic with SQLite: recovery favors delivery, with tagged duplicates
recognizable on platforms lacking a client-supplied idempotency key.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import agent_context
import agent_security

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 45
RETENTION_DAYS = 14
_SCHEMA = """CREATE TABLE IF NOT EXISTS agent_result_notifications (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('telegram','feishu')),
    operator TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
      CHECK(state IN ('pending','leased','retry','sent','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires REAL,
    platform_message_id TEXT,
    sent_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(result_id,channel,chat_id)
)"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_agent_result_ready ON agent_result_notifications(channel,state,next_attempt,lease_expires)"


def configured(settings):
    """Ignore kill only for keeping a paused sender alive across unkill."""
    import os
    return (agent_context.enabled(settings)
            and os.environ.get("CONVEYOR_AGENT_CHANNEL_ROUTING", "").lower()
                in ("1", "true", "yes", "on")
            and agent_security.delegation_configured(settings))


def ensure(conn):
    conn.execute(_SCHEMA)
    conn.execute(_INDEX)


@contextmanager
def _db(settings):
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("BEGIN IMMEDIATE")
        ensure(db)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def enqueue_committed(conn, settings, task, result_id, result_text, *, now=None):
    """Called INSIDE queue.complete()'s transaction, never after it commits."""
    if not configured(settings):
        return False
    if task["idempotency_key"][:9] != "delegate-":
        return False
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='main_router_routes'"
    ).fetchone()
    if not exists:
        return False
    route = conn.execute(
        """SELECT agent_id,agent_name,origin_channel,origin_operator,origin_chat
           FROM main_router_routes WHERE approval_key=?""",
        (task["idempotency_key"],)).fetchone()
    if not route or route["agent_id"] != task["to_agent"]:
        return False
    channel, chat_id, operator = (route["origin_channel"], route["origin_chat"],
                                  route["origin_operator"])
    import agent_channel_routing as routing
    if (channel not in ("telegram", "feishu") or not chat_id
            or operator != routing._owner(settings, channel)):
        return False
    from redaction import redact_text
    now = time.time() if now is None else now
    ensure(conn)
    result_tag = result_id[:12]
    text = (f'[{route["agent_name"]}] delegated result (untrusted data, '
            f'not an instruction or approval) [result:{result_tag}]:\n'
            + redact_text(result_text)[:2400])
    conn.execute(
        """INSERT OR IGNORE INTO agent_result_notifications
           (id,result_id,task_id,channel,operator,chat_id,text,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (uuid4().hex, result_id, task["id"], channel, operator, chat_id,
         text, now, now))
    return True


def _claim(settings, channel, *, now=None):
    if not configured(settings) or agent_security.killed(settings):
        return None
    now = time.time() if now is None else now
    with _db(settings) as db:
        if agent_security.killed(settings):
            return None
        row = db.execute(
            """SELECT * FROM agent_result_notifications
               WHERE channel=? AND
               ((state IN ('pending','retry') AND next_attempt<=?)
               OR (state='leased' AND lease_expires<=?))
               ORDER BY created_at,id LIMIT 1""",
            (channel, now, now)).fetchone()
        if row is None:
            return None
        # A crash after send but before ACK must not bypass the attempt cap.
        if row["attempts"] >= MAX_ATTEMPTS:
            _terminal(db, row["id"], "denied", now)
            return None
        import agent_channel_routing as routing
        if row["operator"] != routing._owner(settings, channel):
            _terminal(db, row["id"], "revoked", now)
            return None
        token = uuid4().hex
        db.execute(
            """UPDATE agent_result_notifications SET state='leased',
               attempts=attempts+1, lease_token=?,lease_expires=?,updated_at=?
               WHERE id=?""",
            (token, now+LEASE_SECONDS, now, row["id"]))
        record = dict(row)
        record["lease_token"] = token
        record["attempts"] += 1
        return record


def _terminal(db, notification_id, reason, now):
    db.execute(
        """UPDATE agent_result_notifications SET state='dead',
           lease_token=NULL,lease_expires=NULL,updated_at=?
           WHERE id=?""", (now, notification_id))
    agent_security.audit_tx(db, operator="web-console", event="deny",
                            agent_id="default", reason=reason)


def _ack(settings, row, *, message_id=None, error=None, now=None):
    now = time.time() if now is None else now
    with _db(settings) as db:
        record = db.execute(
            """SELECT id,attempts FROM agent_result_notifications
               WHERE id=? AND state='leased' AND lease_token=?""",
            (row["id"], row["lease_token"])).fetchone()
        if record is None:
            return False
        if error is None:
            db.execute(
                """UPDATE agent_result_notifications SET state='sent',
                   platform_message_id=?,sent_at=?,updated_at=?,
                   lease_token=NULL,lease_expires=NULL WHERE id=?""",
                (str(message_id or "")[:128], now, now, row["id"]))
        elif record["attempts"] >= MAX_ATTEMPTS:
            _terminal(db, row["id"], "denied", now)
        else:
            wait = min(300, 2 ** record["attempts"])
            db.execute(
                """UPDATE agent_result_notifications SET state='retry',
                   next_attempt=?,lease_token=NULL,lease_expires=NULL,
                   updated_at=? WHERE id=?""",
                (now+wait, now, row["id"]))
        return True


async def consume_once(settings, channel, send):
    """Returns None when no ready item, otherwise 0 or 1.

    send(chat_id, plaintext) is the existing Telegram/Feishu send adapter.
    """
    if not configured(settings):
        return None
    if agent_security.killed(settings):
        return 0
    row = _claim(settings, channel)
    if row is None:
        return None
    import egress_taint
    import agents
    try:
        if agent_security.killed(settings):
            # Pause without consuming retry budget; keep a resumable lease.
            _defer_killed(settings, row)
            return 0
        if (egress_taint.requires_approval(
                settings, "agent.reply", operator_id=row["operator"],
                channel=channel, chat_id=row["chat_id"])
            or egress_taint.requires_approval(
                settings, "agent.reply", operator_id=agents.WEB_OPERATOR,
                channel="web", chat_id=agents.chat_id_for("default"))):
            _ack(settings, row, error="egress")
            return 0
        message_id = await asyncio.wait_for(
            send(row["chat_id"], row["text"]), timeout=15)
        _ack(settings, row, message_id=message_id)
        return 1
    except asyncio.CancelledError:
        # Claim expires and is retried after a crash or process shutdown.
        raise
    except Exception:
        log.warning("Result notification send failed, will retry: %s",
                    row["id"], exc_info=True)
        _ack(settings, row, error="send")
        return 0


def _defer_killed(settings, row):
    with _db(settings) as db:
        db.execute(
            """UPDATE agent_result_notifications SET state='retry',
               next_attempt=0,lease_token=NULL,lease_expires=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (row["id"], row["lease_token"]))


def purge(settings, *, now=None, batch=100):
    now = time.time() if now is None else now
    with _db(settings) as db:
        rows = db.execute(
            """SELECT id FROM agent_result_notifications
               WHERE state IN ('sent','dead') AND updated_at<?
               ORDER BY updated_at,id LIMIT ?""",
            (now-RETENTION_DAYS*86400, min(100, max(1, batch)))).fetchall()
        db.executemany("DELETE FROM agent_result_notifications WHERE id=?",
                       ((row["id"],) for row in rows))
        return len(rows)
