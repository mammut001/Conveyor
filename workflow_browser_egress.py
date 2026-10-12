"""Privacy-safe, fail-closed SQLite egress audit for the G1 read-only browser."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import time


class EgressAuditUnavailable(RuntimeError):
    """No audited outbound request can proceed if storage is unavailable."""


def private_database_path(path: str | Path) -> Path:
    """Require an operator-owned, private directory and reject symlink DBs."""
    db = Path(path).expanduser().absolute()
    try:
        db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = db.parent.stat()
        if (stat.S_IMODE(parent.st_mode) & 0o077) or parent.st_uid != os.getuid():
            raise EgressAuditUnavailable("insecure_database_directory")
        if db.is_symlink():
            raise EgressAuditUnavailable("symlink_database_denied")
        fd = os.open(str(db), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if stat.S_IMODE(db.stat().st_mode) & 0o077:
            raise EgressAuditUnavailable("insecure_database_permissions")
    except (OSError, ValueError) as exc:
        raise EgressAuditUnavailable("audit_storage_unavailable") from exc
    return db


class EgressAudit:
    """Records only fixed-code outcomes; never URLs, headers, bodies or page text."""

    def __init__(self, path: str | Path) -> None:
        self.path = private_database_path(path)
        try:
            with sqlite3.connect(self.path, timeout=5) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS browser_external_egress (
                    id INTEGER PRIMARY KEY, timestamp REAL NOT NULL,
                    task TEXT NOT NULL, domain TEXT NOT NULL,
                    method TEXT NOT NULL, outcome TEXT NOT NULL,
                    reason TEXT NOT NULL
                )""")
        except sqlite3.Error as exc:
            raise EgressAuditUnavailable("audit_storage_unavailable") from exc

    def record(self, task: str, domain: str, method: str,
               outcome: str, reason: str) -> None:
        if outcome not in ("allowed", "denied"):
            raise ValueError("invalid_egress_outcome")
        # Inputs must be validated/canonicalized by the policy caller.
        if (method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OTHER")
                or not reason.isascii() or not reason.replace("_", "").isalnum()):
            raise ValueError("invalid_egress_record")
        try:
            with sqlite3.connect(self.path, timeout=2) as conn:
                conn.execute(
                    """INSERT INTO browser_external_egress
                       (timestamp, task, domain, method, outcome, reason)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (time.time(), task, domain, method, outcome, reason),
                )
        except sqlite3.Error as exc:
            raise EgressAuditUnavailable("audit_storage_unavailable") from exc
