"""Atomic delegation-specific mailbox claims and stop transitions."""
from __future__ import annotations

import time
from uuid import uuid4

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
