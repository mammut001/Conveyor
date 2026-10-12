"""Secret-free two-phase journal for fixture browser actions.

A dispatched-but-unverified action is NEVER replayed automatically: another
process may have performed the side effect just before a crash.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator


# Deliberately fixed codes; never persist raw exception text, URL or DOM.
DENIAL_CODES = frozenset({
    "origin_denied", "unsafe_http_method", "fixture_content_mismatch",
    "declarative_shadow_denied", "unsafe_fixture_markup",
    "unsafe_html_api_denied", "webrtc_denied",
    "worker_denied", "document_write_denied", "cdp_dom_unavailable",
    "request_filter_error", "websocket_denied", "websocket_routing_unavailable",
    "popup_denied", "download_denied", "closed_shadow_denied",
    "uninspectable_shadow_host", "sensitive_or_submit_dom_denied",
    "sensitive_autocomplete_denied", "browser_policy_violation",
    "real_browser_disabled", "page_not_allowlisted", "fixture_marker_mismatch",
    "unsafe_field", "sensitive_field_denied", "target_denied",
    "unsafe_navigation_control", "action_denied", "field_assertion_failed",
    "marker_denied", "page_precondition_failed",
})


class JournalConflict(Exception):
    pass


class BrowserAttemptJournal:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or parent.stat().st_mode & 0o077:
            raise ValueError("journal parent must be private")
        try:
            fd = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValueError("journal database must be a private regular file")
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS browser_action_attempts (
                    plan_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    plan_digest TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('dispatched','verified')),
                    before_page TEXT NOT NULL,
                    after_page TEXT NOT NULL,
                    denial_reason TEXT,
                    PRIMARY KEY (plan_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS browser_workflow_bindings (
                    plan_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    step_ordinal INTEGER NOT NULL,
                    plan_digest TEXT NOT NULL,
                    UNIQUE(workflow_id, step_ordinal)
                );
            """)
            # Backward-compatible migration of journals created by PR #108.
            cols = {row["name"] for row in db.execute("PRAGMA table_info(browser_action_attempts)")}
            if "denial_reason" not in cols:
                db.execute("ALTER TABLE browser_action_attempts ADD COLUMN denial_reason TEXT")

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind_workflow(self, plan_id: str, workflow_id: str,
                      step_ordinal: int, plan_digest: str) -> None:
        """Permanently bind an immutable plan to one workflow/step in SQLite."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT plan_id,workflow_id,step_ordinal,plan_digest FROM browser_workflow_bindings "
                "WHERE plan_id=? OR (workflow_id=? AND step_ordinal=?)",
                (plan_id, workflow_id, step_ordinal),
            ).fetchone()
            if row is not None:
                if tuple(row) != (plan_id, workflow_id, step_ordinal, plan_digest):
                    raise JournalConflict("plan is already bound to a different checkpoint")
                return
            db.execute(
                "INSERT INTO browser_workflow_bindings VALUES (?,?,?,?)",
                (plan_id, workflow_id, step_ordinal, plan_digest),
            )

    def state(self, plan_id: str, ordinal: int) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT plan_digest,kind,status,before_page,after_page,denial_reason "
                "FROM browser_action_attempts WHERE plan_id=? AND ordinal=?",
                (plan_id, ordinal),
            ).fetchone()
            return dict(row) if row else None

    def dispatch(self, plan_id: str, ordinal: int, digest: str, kind: str,
                 before: str, after: str) -> str:
        """Claim the operation atomically before invoking any mutating driver."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT plan_digest,kind,status,before_page,after_page "
                "FROM browser_action_attempts WHERE plan_id=? AND ordinal=?",
                (plan_id, ordinal),
            ).fetchone()
            if row:
                if (row["plan_digest"], row["kind"], row["before_page"], row["after_page"]) != (
                    digest, kind, before, after
                ):
                    raise JournalConflict("attempt bound to a different immutable action plan")
                return str(row["status"])
            db.execute(
                "INSERT INTO browser_action_attempts "
                "(plan_id,ordinal,plan_digest,kind,status,before_page,after_page) "
                "VALUES (?,?,?,?,?,?,?)",
                (plan_id, ordinal, digest, kind, "dispatched", before, after),
            )
            return "dispatched_new"

    def record_denial(self, plan_id: str, ordinal: int, digest: str, reason: str) -> None:
        """Bind the first safe filter reason to an uncertain dispatched action."""
        if reason not in DENIAL_CODES:
            raise ValueError("unsupported denial reason")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE browser_action_attempts SET denial_reason=? "
                "WHERE plan_id=? AND ordinal=? AND plan_digest=? "
                "AND status='dispatched' AND denial_reason IS NULL",
                (reason, plan_id, ordinal, digest),
            )

    def verify(self, plan_id: str, ordinal: int, digest: str) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE browser_action_attempts SET status='verified' "
                "WHERE plan_id=? AND ordinal=? AND plan_digest=? AND status='dispatched'",
                (plan_id, ordinal, digest),
            ).rowcount
            if changed != 1:
                raise JournalConflict("cannot verify unknown or already finalized attempt")

    def summaries(self, plan_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT ordinal,kind,status,before_page,after_page,denial_reason "
                "FROM browser_action_attempts WHERE plan_id=? ORDER BY ordinal", (plan_id,),
            ).fetchall()
            return [dict(row) for row in rows]
