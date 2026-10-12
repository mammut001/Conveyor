"""Recover unknown network outcomes without reissuing a single submit request."""
from __future__ import annotations
import os
import secrets

# Stable for the lifetime of this process; new on restart, including a new PID.
INSTANCE = f"{os.getpid()}-{secrets.token_hex(16)}"


def recover(conn) -> None:
    """Migrate old rows, then mark foreign/ownerless dispatched records uncertain.

    Same-process in-flight requests keep their dispatch owner and can complete.
    A process restart obtains a new owner; these entries must never be replayed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(browser_g3_submits)")}
        if "dispatch_owner" not in columns:
            conn.execute("ALTER TABLE browser_g3_submits ADD COLUMN dispatch_owner TEXT")
        conn.execute("""UPDATE browser_g3_submits SET state='needs_reconciliation'
            WHERE state='dispatched' AND
                  (dispatch_owner IS NULL OR dispatch_owner != ?)""", (INSTANCE,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
