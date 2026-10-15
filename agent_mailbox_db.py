"""Transactional SQLite storage for the opt-in agent mailbox.

The mailbox shares AgentStore's job_queue.sqlite3, but creates no schema unless
the mailbox feature is enabled and an authorized mailbox API is used.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_mailbox (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    operator TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('message','task_request','task_result')),
    body TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','leased','done','failed','dead')),
    lease_owner TEXT,
    lease_expires_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 8),
    deadline REAL NOT NULL,
    created_at REAL NOT NULL,
    parent_message_id TEXT,
    depth INTEGER NOT NULL CHECK(depth BETWEEN 0 AND 1),
    UNIQUE(from_agent, to_agent, idempotency_key),
    FOREIGN KEY(parent_message_id) REFERENCES agent_mailbox(id)
)
"""
_INDEX = """CREATE INDEX IF NOT EXISTS idx_agent_mailbox_delivery
    ON agent_mailbox(to_agent, operator, status, created_at)"""


@contextmanager
def transaction(settings) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE serializes migrations, claims and state transitions."""
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        # Idempotent migration stays inside the *same* write transaction.
        connection.execute(_SCHEMA)
        connection.execute(_INDEX)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_active(conn: sqlite3.Connection, *agent_ids: str) -> None:
    """Recheck archived status inside the same transaction as any mutation."""
    for agent_id in agent_ids:
        row = conn.execute(
            "SELECT archived FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        if row is None or row["archived"]:
            raise ValueError("agent missing or archived")


def public_row(row: sqlite3.Row) -> dict:
    """Message text is *always* data; never an instruction or approval."""
    data = dict(row)
    data["trust_level"] = "untrusted_agent_message"
    data["tainted"] = True
    data["approval_authority"] = False
    data["instruction_authority"] = False
    return data


def expire(conn: sqlite3.Connection, to_agent: str, operator: str,
           now: float) -> None:
    """Close expired and exhausted items without ever re-executing them."""
    conn.execute(
        """UPDATE agent_mailbox SET status='dead', lease_owner=NULL,
           lease_expires_at=NULL
           WHERE to_agent=? AND operator=?
           AND status IN ('queued','failed','leased')
           AND (deadline<=? OR
                (status='leased' AND lease_expires_at<=?
                 AND attempts>=max_attempts))""",
        (to_agent, operator, now, now),
    )
