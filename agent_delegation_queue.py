"""Atomic delegation-specific mailbox claims and stop transitions."""
from __future__ import annotations

import time
from uuid import uuid4
import agent_context as ac

import agent_mailbox_db as db

MAX_CONCURRENT_PER_OPERATOR = 2


def claim(settings, target, *, now=None):
    current = time.time() if now is None else now
    with db.transaction(settings) as conn:
        db.ensure_active(conn, target.agent_id)
        db.expire(conn, target.agent_id, target.operator_identity, current)
        # Serialize cap accounting and claims with BEGIN IMMEDIATE, so two
        # processes cannot bypass the same-operator child limit.
        active = conn.execute(
            """SELECT COUNT(*) FROM agent_mailbox
               WHERE operator=? AND kind='task' AND status='leased'
               AND lease_expires_at>? AND deadline>?""",
            (target.operator_identity, current, current)).fetchone()[0]
        if active >= MAX_CONCURRENT_PER_OPERATOR:
            return None
        row = conn.execute(
            """SELECT * FROM agent_mailbox WHERE operator=? AND to_agent=?
               AND kind='task' AND depth=0 AND deadline>?
               AND attempts<max_attempts AND
               (status IN ('queued','failed') OR
                (status='leased' AND lease_expires_at<=?))
               ORDER BY created_at,id LIMIT 1""",
            (target.operator_identity, target.agent_id, current, current)).fetchone()
        if row is None:
            return None
        worker_id = uuid4().hex
        conn.execute(
            """UPDATE agent_mailbox SET status='leased', lease_owner=?,
               lease_expires_at=?, attempts=attempts+1 WHERE id=?""",
            (worker_id, min(current+120, row["deadline"]), row["id"]))
        record = db.public_row(conn.execute(
            "SELECT * FROM agent_mailbox WHERE id=?", (row["id"],)).fetchone())
        return record, worker_id


def cancel_operator(settings, operator):
    """Safety stop is authoritative; do not requeue cancelled tasks."""
    if not operator:
        return 0
    with db.transaction(settings) as conn:
        found = conn.execute(
            """SELECT COUNT(*) FROM agent_mailbox WHERE operator=? AND kind='task'
               AND status IN ('queued','failed','leased')""", (operator,)
        ).fetchone()[0]
        conn.execute(
            """UPDATE agent_mailbox SET status='dead', lease_owner=NULL,
               lease_expires_at=NULL WHERE operator=? AND kind='task'
               AND status IN ('queued','failed','leased')""", (operator,))
        return found


def complete(settings, target, record, worker_id, body):
    """Atomically deliver ONE result and ack only a live, owned task lease.

    A concurrent emergency stop wins or loses the same SQLite write lock:
    it cannot leave a 'done' task without a result or emit after a stop.
    """
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 8192:
        raise ValueError("invalid delegation result")
    if ac.current() != target:
        raise ValueError("unbound delegation target")
    ac.validated(settings, target, target.origin_channel, ac._chat_id(target),
                 target.operator_identity)
    now = time.time()
    with db.transaction(settings) as conn:
        db.ensure_active(conn, target.agent_id, record["from_agent"])
        row = conn.execute(
            """SELECT * FROM agent_mailbox WHERE id=? AND kind='task'
               AND status='leased' AND lease_owner=? AND to_agent=?
               AND operator=? AND deadline>? AND lease_expires_at>?""",
            (record["id"], worker_id, target.agent_id, target.operator_identity,
             now, now)).fetchone()
        if row is None:
            return False
        key = "delegate-result-" + row["id"]
        prior = conn.execute(
            """SELECT id FROM agent_mailbox
               WHERE from_agent=? AND to_agent=? AND idempotency_key=?""",
            (row["to_agent"], row["from_agent"], key)).fetchone()
        if prior is None:
            conn.execute(
                """INSERT INTO agent_mailbox (
                  id,from_agent,to_agent,operator,kind,body,idempotency_key,
                  status,attempts,max_attempts,deadline,created_at,
                  parent_message_id,depth
                ) VALUES (?,?,?,?,?,?,?,'queued',0,3,?,?,?,1)""",
                (uuid4().hex,row["to_agent"],row["from_agent"],row["operator"],
                 "result",body,key,now+3600,now,row["id"]))
        conn.execute(
            """UPDATE agent_mailbox SET status='done',
               lease_owner=NULL,lease_expires_at=NULL WHERE id=?""",
            (row["id"],))
        return True


def reject_revoked(settings, record, worker_id):
    """Archive/revoke after claim: close the lease without needing old context."""
    with db.transaction(settings) as conn:
        conn.execute(
            """UPDATE agent_mailbox SET status='dead',lease_owner=NULL,
               lease_expires_at=NULL WHERE id=? AND kind='task' AND
               status='leased' AND lease_owner=? AND to_agent=? AND operator=?""",
            (record["id"], worker_id, record["to_agent"], record["operator"]))
