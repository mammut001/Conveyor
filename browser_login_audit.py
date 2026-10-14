"""One-hour per-origin login attempt quotas and secret-free audit."""
from __future__ import annotations
import sqlite3
import time

from workflow_browser_egress import private_database_path

_STATUSES = {"started", "logged_in", "failed", "needs_human"}


class LoginQuotaDenied(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _connect(settings):
    db = private_database_path(settings.codex_memory_root / "browser_external" /
                               "browser_login_attempts.sqlite3")
    conn = sqlite3.connect(db, timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS login_attempts
                (id INTEGER PRIMARY KEY, domain TEXT NOT NULL,
                 username TEXT NOT NULL, result TEXT NOT NULL,
                 time REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS login_attempts_domain_time ON login_attempts(domain,time)")
    return conn


def reserve(settings, domain, username):
    """BEGIN IMMEDIATE makes three-concurrent-attempt bypass impossible."""
    with _connect(settings) as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute("""SELECT result FROM login_attempts
            WHERE domain=? AND time>? ORDER BY time DESC""",
            (domain, time.time() - 3600)).fetchall()
        if sum(x[0] == "failed" for x in rows) >= 3:
            raise LoginQuotaDenied("locked")
        if len(rows) >= 3:
            raise LoginQuotaDenied("rate_limited")
        cursor = c.execute("INSERT INTO login_attempts(domain,username,result,time) VALUES(?,?,?,?)",
                           (domain, username, "started", time.time()))
        return cursor.lastrowid


def finish(settings, attempt_id, result):
    if result not in _STATUSES:
        raise ValueError("invalid result")
    with _connect(settings) as c:
        c.execute("UPDATE login_attempts SET result=? WHERE id=?",
                  (result, attempt_id))


def public_audit(settings, domain):
    """Optional audit view without passwords, URLs, screenshots or cookies."""
    with _connect(settings) as c:
        return [{"domain": d, "username": u, "result": r, "time": t}
                for d, u, r, t in c.execute(
                    "SELECT domain,username,result,time FROM login_attempts WHERE domain=? ORDER BY id",
                    (domain,))]
