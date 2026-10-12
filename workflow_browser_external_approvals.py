"""Task-scoped, out-of-band operator approval ledger for external read-only mode."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import time

from workflow_browser_egress import private_database_path
from workflow_browser_external_identity import (
    ExternalPolicyDenied, PATH_RE, canonical_host, canonical_url,
)

TASK_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
APPROVER_RE = re.compile(r"[a-zA-Z0-9_.@-]{1,100}\Z")

@dataclass(frozen=True)
class ApprovedSite:
    task: str
    host: str
    port: int
    expires_at: float
    safe_paths: frozenset[str]
    methods: frozenset[str]

    @property
    def origin(self) -> str:
        return f"https://{self.host}" + (f":{self.port}" if self.port != 443 else "")

    def check_url(self, url: str, method: str) -> None:
        host, port, path = canonical_url(url)
        if host != self.host or port != self.port:
            raise ExternalPolicyDenied("origin_denied")
        if path not in self.safe_paths:
            raise ExternalPolicyDenied("path_denied")
        if method not in self.methods:
            raise ExternalPolicyDenied("unsafe_http_method")

class ApprovalLedger:
    """Only the trusted operator call site can write; a website cannot approve itself."""
    def __init__(self, path: str | Path) -> None:
        self.path = private_database_path(path)
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS browser_external_approvals (
                task TEXT PRIMARY KEY, host TEXT NOT NULL, port INTEGER NOT NULL,
                expires_at REAL NOT NULL, approver TEXT NOT NULL,
                safe_paths TEXT NOT NULL, methods TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )""")

    def approve(self, *, task: str, host: str, port: int = 443,
                expires_at: float, approver: str,
                safe_paths: tuple[str, ...], methods: tuple[str, ...] = ("GET",)) -> None:
        host = canonical_host(host)
        if (not isinstance(task, str) or not TASK_RE.fullmatch(task)
                or not isinstance(approver, str) or not APPROVER_RE.fullmatch(approver)
                or not isinstance(port, int) or isinstance(port, bool)
                or not 1 <= port <= 65535):
            raise ExternalPolicyDenied("invalid_approval")
        if not time.time() < expires_at <= time.time() + 3600:
            raise ExternalPolicyDenied("invalid_approval_expiry")
        if (not safe_paths or len(safe_paths) > 20 or any(
                not isinstance(p, str) or not PATH_RE.fullmatch(p)
                or "//" in p or ".." in p.split("/") for p in safe_paths)):
            raise ExternalPolicyDenied("invalid_safe_paths")
        if not methods or not set(methods).issubset({"GET", "HEAD"}):
            raise ExternalPolicyDenied("invalid_safe_methods")
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute("""INSERT OR REPLACE INTO browser_external_approvals
                (task,host,port,expires_at,approver,safe_paths,methods,revoked)
                VALUES (?,?,?,?,?,?,?,0)""",
                (task, host, port, expires_at, approver,
                 json.dumps(sorted(set(safe_paths))), json.dumps(sorted(set(methods)))))

    def revoke(self, task: str) -> None:
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute("UPDATE browser_external_approvals SET revoked=1 WHERE task=?",
                         (task,))

    def require(self, task: str) -> ApprovedSite:
        if not isinstance(task, str) or not TASK_RE.fullmatch(task):
            raise ExternalPolicyDenied("approval_missing")
        try:
            with sqlite3.connect(self.path, timeout=2) as conn:
                row = conn.execute("""SELECT host,port,expires_at,safe_paths,methods,revoked
                    FROM browser_external_approvals WHERE task=?""", (task,)).fetchone()
        except sqlite3.Error as exc:
            raise ExternalPolicyDenied("approval_store_unavailable") from exc
        if row is None:
            raise ExternalPolicyDenied("approval_missing")
        if row[5]:
            raise ExternalPolicyDenied("approval_revoked")
        if row[2] <= time.time():
            raise ExternalPolicyDenied("approval_expired")
        return ApprovedSite(task, row[0], row[1], row[2],
                            frozenset(json.loads(row[3])),
                            frozenset(json.loads(row[4])))
