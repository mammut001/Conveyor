"""One-hour per-origin credential-rejection quota with separate infra cooldown.

Reasons and proof-of-submission flags are metadata only: no credentials, page
content, request bodies or exception messages are stored.
"""
from __future__ import annotations
import re
import sqlite3
import time

from browser_login_quota_policy import admission_reason
from workflow_browser_egress import private_database_path

_STATUSES = {"started", "logged_in", "failed", "needs_human"}
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


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
                 time REAL NOT NULL,
                 reason TEXT NOT NULL DEFAULT 'legacy_unverified',
                 submitted INTEGER NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(login_attempts)")}
    # Treat pre-migration generic failed rows as possible wrong passwords:
    # an existing three-failure lockout must survive schema migration.
    if "reason" not in columns:
        conn.execute("ALTER TABLE login_attempts ADD COLUMN reason TEXT NOT NULL DEFAULT 'legacy_unverified'")
    if "submitted" not in columns:
        conn.execute("ALTER TABLE login_attempts ADD COLUMN submitted INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS login_attempts_domain_time ON login_attempts(domain,time)")
    return conn


def reserve(settings, domain, username):
    """Atomic quota admission across processes; pending attempts briefly reserve capacity."""
    with _connect(settings) as c:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        rows = c.execute("""SELECT result,reason,submitted,time FROM login_attempts
            WHERE domain=? AND time>? ORDER BY time DESC""",
            (domain, now - 3600)).fetchall()
        denied = admission_reason(rows, now)
        if denied:
            raise LoginQuotaDenied(denied)
        cursor = c.execute("""INSERT INTO login_attempts
            (domain,username,result,time,reason,submitted)
            VALUES(?,?,?,?,?,0)""",
            (domain, username, "started", now, "in_progress"))
        return cursor.lastrowid


def mark_submitted(settings, attempt_id):
    """Commit the admission charge BEFORE any credential-bearing network I/O.

    If the audit is missing/unavailable, the caller must abort the POST.
    A crash after this commit retains a started+submitted=1 charge.
    """
    with _connect(settings) as c:
        cursor = c.execute("""UPDATE login_attempts
            SET submitted=1, reason='post_dispatched'
            WHERE id=? AND result='started' AND submitted=0""", (attempt_id,))
        if cursor.rowcount != 1:
            raise ValueError("login_audit_attempt_unavailable")


def finish(settings, attempt_id, result, *, reason=None, submitted=False):
    if result not in _STATUSES:
        raise ValueError("invalid result")
    reason = reason or ("success" if result == "logged_in" else "unspecified")
    if not isinstance(reason, str) or not _REASON.fullmatch(reason):
        raise ValueError("invalid reason")
    if reason == "credentials_rejected" and (result != "failed" or not submitted):
        raise ValueError("unverified rejection")
    with _connect(settings) as c:
        # A pre-dispatch committed marker can never be cleared by a timed-out
        # handler, teardown error or stale in-memory session state.
        c.execute("""UPDATE login_attempts
                     SET result=?,reason=?,submitted=MAX(submitted,?)
                     WHERE id=?""",
                  (result, reason, int(bool(submitted)), attempt_id))


def public_audit(settings, domain):
    """Fixed metadata only; never expose passwords, cookies or page content."""
    with _connect(settings) as c:
        return [{"domain": d, "username": u, "result": r, "time": t,
                 "reason": why, "submitted": bool(submitted)}
                for d, u, r, t, why, submitted in c.execute(
                    """SELECT domain,username,result,time,reason,submitted
                       FROM login_attempts WHERE domain=? ORDER BY id""",
                    (domain,))]
