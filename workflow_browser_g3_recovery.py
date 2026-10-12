"""Recover unknown network outcomes without reissuing a single submit request."""
from __future__ import annotations
import os
import secrets

# Stable per process lifetime; a fork/restart must not inherit the same owner.
_BOOT_NONCE = secrets.token_hex(16)


def owner() -> str:
    return f"{os.getpid()}-{_BOOT_NONCE}"


def _alive(identifier: str | None) -> bool:
    if not identifier:
        return False
    try:
        pid = int(identifier.split("-", 1)[0])
        if pid <= 0:
            return False
        if pid == os.getpid():
            return identifier == owner()  # PID reuse or inherited fork
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # No right to inspect is not proof of death.
    except (ValueError, OverflowError, OSError):
        return False


def recover(conn) -> None:
    """Mark orphaned dispatched outcomes uncertain; never replay.

    Do not disturb active requests owned by another running local process
    (e.g. the Telegram worker while the Web Console lists approvals).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(browser_g3_submits)")}
        if "dispatch_owner" not in columns:
            conn.execute("ALTER TABLE browser_g3_submits ADD COLUMN dispatch_owner TEXT")
        rows = conn.execute("""SELECT token,dispatch_owner FROM browser_g3_submits
            WHERE state='dispatched'""").fetchall()
        for token, dispatch_owner in rows:
            if not _alive(dispatch_owner):
                conn.execute("""UPDATE browser_g3_submits SET state='needs_reconciliation'
                    WHERE token=? AND state='dispatched'""", (token,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
