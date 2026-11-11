"""Durable, explicitly-operated workflow checkpoints.

This module stores *only* short operator-authored task labels and progress.
It intentionally executes no browser actions, file uploads, payments or submissions.
Existing CUA / Human Takeover policies remain the sole authority for those.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Iterator
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from redaction import redact_text

KINDS = frozenset({"work", "browser", "documents", "human", "submission"})
HUMAN_KINDS = frozenset({"human", "submission"})
ACTIONS = frozenset({
    "begin", "complete", "fail", "retry", "request_human", "confirm_human",
    "pause", "resume", "resume_verified", "cancel",
})
MAX_STEPS = 40
MAX_ATTEMPTS = 5
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,95}$")
_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class WorkflowConflict(Exception):
    """A stale revision, used request ID, or illegal state transition."""


class WorkflowNotFound(Exception):
    pass


def _label(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("title and step labels must be strings")
    clean = value.strip()
    if not clean or len(clean) > limit or any(ord(c) < 32 or ord(c) == 127 for c in clean):
        raise ValueError("invalid title or step label")
    # Labels belong in an operator-visible checklist, never in a secret vault.
    if redact_text(clean) != clean:
        raise ValueError("workflow labels cannot contain credentials or tokens")
    return clean


def _key(request_id: Any) -> str:
    if not isinstance(request_id, str) or not _KEY_RE.fullmatch(request_id):
        raise ValueError("request_id must be 8-96 URL-safe characters")
    return request_id


def _plan(title: Any, steps: Any) -> tuple[str, list[tuple[str, str]], str]:
    clean_title = _label(title, 100)
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("steps must contain 1-40 entries")
    clean_steps: list[tuple[str, str]] = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"kind", "label"}:
            raise ValueError("each step must have kind and label only")
        kind = step["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("unsupported step kind")
        clean_steps.append((kind, _label(step["label"], 120)))
    serialized = json.dumps([clean_title, clean_steps], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return clean_title, clean_steps, digest


class WorkflowStore:
    """SQLite CAS + request-idempotency store, safe across Web worker processes."""

    def __init__(self, settings: Any):
        if not getattr(settings, "workflows_enabled", False):
            raise ValueError("workflows are disabled")
        root = Path(settings.codex_memory_root) / "state"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / "workflows.sqlite3"
        # Create the database at 0600 *before* SQLite creates WAL/SHM sidecars.
        # Refuse symlinks and pre-existing insecure files, rather than chmodding
        # an arbitrary operator path after opening it.
        try:
            fd = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
        if self.path.is_symlink() or (self.path.stat().st_mode & 0o077):
            raise ValueError("workflow database path must be a private regular file")
        if not self.path.is_file():
            raise ValueError("workflow database path is not a regular file")
        self._initialize()

    def _db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._db()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
                    revision INTEGER NOT NULL, plan_hash TEXT NOT NULL,
                    create_request_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workflow_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL, action TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL, result_json TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    PRIMARY KEY (workflow_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflows_updated ON workflows(updated_at DESC);
            """)

    @staticmethod
    def _view(db: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise WorkflowNotFound()
        steps = db.execute(
            "SELECT ordinal,kind,label,state,attempts FROM workflow_steps "
            "WHERE workflow_id=? ORDER BY ordinal", (workflow_id,),
        ).fetchall()
        return {
            "id": row["id"], "title": row["title"], "state": row["state"],
            "revision": row["revision"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "steps": [dict(step) for step in steps],
        }

    def create(self, *, title: str, steps: list[dict[str, str]], request_id: str) -> dict[str, Any]:
        rid = _key(request_id)
        title, normalized, digest = _plan(title, steps)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prev = db.execute(
                "SELECT id,plan_hash FROM workflows WHERE create_request_id=?", (rid,),
            ).fetchone()
            if prev:
                if prev["plan_hash"] != digest:
                    raise WorkflowConflict("request_id already used for a different plan")
                return self._view(db, prev["id"])
            wf_id = secrets.token_hex(16)
            now = time.time()
            first_state = "waiting_for_human" if normalized[0][0] in HUMAN_KINDS else "ready"
            overall = "waiting_for_human" if first_state == "waiting_for_human" else "active"
            db.execute(
                "INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?)",
                (wf_id, title, overall, 0, digest, rid, now, now),
            )
            db.executemany(
                "INSERT INTO workflow_steps (workflow_id,ordinal,kind,label,state) VALUES (?,?,?,?,?)",
                [
                    (wf_id, i, kind, label, first_state if i == 0 else "pending")
                    for i, (kind, label) in enumerate(normalized)
                ],
            )
            return self._view(db, wf_id)

    def get(self, workflow_id: str) -> dict[str, Any]:
        if not isinstance(workflow_id, str) or not _ID_RE.fullmatch(workflow_id):
            raise WorkflowNotFound()
        with self._connection() as db:
            return self._view(db, workflow_id)

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        count = min(max(int(limit), 1), 50)
        with self._connection() as db:
            ids = db.execute(
                "SELECT id FROM workflows ORDER BY updated_at DESC LIMIT ?", (count,),
            ).fetchall()
            return [self._view(db, row["id"]) for row in ids]

    def transition(
        self, workflow_id: str, *, action: str, expected_revision: int, request_id: str,
    ) -> dict[str, Any]:
        rid = _key(request_id)
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError("unsupported action")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(workflow_id, str) or not _ID_RE.fullmatch(workflow_id):
            raise WorkflowNotFound()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,revision FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None:
                raise WorkflowNotFound()
            old = db.execute(
                "SELECT action,expected_revision,result_json FROM workflow_events "
                "WHERE workflow_id=? AND request_id=?", (workflow_id, rid),
            ).fetchone()
            if old:
                if old["action"] != action or old["expected_revision"] != expected_revision:
                    raise WorkflowConflict("request_id already used for a different transition")
                result = json.loads(old["result_json"])
                result["replayed"] = True
                return result
            if row["revision"] != expected_revision:
                raise WorkflowConflict("stale revision; reload workflow")
            state = row["state"]
            if state in ("completed", "cancelled"):
                raise WorkflowConflict("workflow is terminal")
            step = db.execute(
                "SELECT * FROM workflow_steps WHERE workflow_id=? AND state!='completed' "
                "ORDER BY ordinal LIMIT 1", (workflow_id,),
            ).fetchone()
            if step is None:
                raise WorkflowConflict("no active step")
            step_state = step["state"]
            step_kind = step["kind"]
            ordinal = step["ordinal"]
            attempts = step["attempts"]
            new_state = state
            new_step = step_state
            if action == "cancel":
                new_state = "cancelled"
                new_step = "cancelled"
            elif action == "pause":
                if state in ("paused", "waiting_for_human"):
                    raise WorkflowConflict("cannot pause a paused/human-owned workflow")
                new_state = "paused"
            elif action == "resume":
                if state != "paused":
                    raise WorkflowConflict("workflow is not paused")
                new_state = (
                    "failed" if step_state == "failed"
                    else "needs_reverification" if step_state == "needs_reverification"
                    else "active"
                )
            else:
                if state == "paused":
                    raise WorkflowConflict("workflow is paused")
                if action == "resume_verified":
                    # A human took over *part* of a normal running step.
                    # Confirming the handoff alone cannot complete the step;
                    # operator must explicitly recheck the remaining work.
                    if step_state != "needs_reverification" or state != "needs_reverification":
                        raise WorkflowConflict("no interrupted step awaiting reverification")
                    new_step, new_state = "running", "active"
                elif action == "begin":
                    if step_state != "ready" or step_kind in HUMAN_KINDS or attempts >= MAX_ATTEMPTS:
                        raise WorkflowConflict("step cannot begin")
                    new_step = "running"
                    attempts += 1
                    new_state = "active"
                elif action == "complete":
                    if step_state != "running" or step_kind in HUMAN_KINDS:
                        raise WorkflowConflict("step cannot complete")
                    new_step = "completed"
                elif action == "fail":
                    if step_state != "running":
                        raise WorkflowConflict("step is not running")
                    new_step, new_state = "failed", "failed"
                elif action == "retry":
                    if step_state != "failed" or attempts >= MAX_ATTEMPTS:
                        raise WorkflowConflict("retry not permitted")
                    new_step, new_state = "ready", "active"
                elif action == "request_human":
                    if step_state != "running":
                        raise WorkflowConflict("only a running step can request handoff")
                    new_step, new_state = "waiting_for_human", "waiting_for_human"
                elif action == "confirm_human":
                    if step_state != "waiting_for_human":
                        raise WorkflowConflict("no human checkpoint pending")
                    if step_kind in HUMAN_KINDS:
                        # Human-only checklist item: operator confirmation
                        # completes just this checkpoint, not a real submission.
                        new_step = "completed"
                    else:
                        # Handoff interrupted a running browser/work/documents
                        # step: DO NOT silently skip unfinished work after MFA.
                        new_step, new_state = "needs_reverification", "needs_reverification"
                if new_step == "completed":
                    following = db.execute(
                        "SELECT ordinal,kind FROM workflow_steps "
                        "WHERE workflow_id=? AND ordinal>? ORDER BY ordinal LIMIT 1",
                        (workflow_id, ordinal),
                    ).fetchone()
                    if following:
                        next_state = "waiting_for_human" if following["kind"] in HUMAN_KINDS else "ready"
                        db.execute(
                            "UPDATE workflow_steps SET state=? WHERE workflow_id=? AND ordinal=?",
                            (next_state, workflow_id, following["ordinal"]),
                        )
                        new_state = "waiting_for_human" if next_state == "waiting_for_human" else "active"
                    else:
                        new_state = "completed"
            db.execute(
                "UPDATE workflow_steps SET state=?, attempts=? WHERE workflow_id=? AND ordinal=?",
                (new_step, attempts, workflow_id, ordinal),
            )
            db.execute(
                "UPDATE workflows SET state=?, revision=revision+1, updated_at=? WHERE id=?",
                (new_state, time.time(), workflow_id),
            )
            result = self._view(db, workflow_id)
            db.execute(
                "INSERT INTO workflow_events VALUES (?,?,?,?,?,?)",
                (workflow_id, rid, action, expected_revision,
                 json.dumps(result, ensure_ascii=False, separators=(",", ":")), time.time()),
            )
            return result
