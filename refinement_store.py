"""Persistent active-worktree refinement state for Conveyor sessions.

A refinement chain is session-scoped control-plane state: multiple independent
queue/runtime jobs may share one worktree until Apply or Discard closes it.
The store deliberately lives in the existing job_queue.sqlite3 database so
queue recovery and refinement recovery have the same persistence boundary.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from transcript_store import session_identity

ACTIVE = "active"
APPLIED = "applied"
DISCARDED = "discarded"
STALE = "stale"
_CLOSED_STATES = {APPLIED, DISCARDED, STALE}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RefinementMutation:
    """SQLite write-lock guard for Apply/Discard vs queue dequeue races.

    A live guard keeps a BEGIN IMMEDIATE transaction open on the same control
    DB used by JobQueue. Queue enqueue/dequeue also require a write transaction,
    so a continuation cannot transition to running between the idle check and
    the final chain close. Failed Apply validation rolls the guard back and
    leaves the chain active.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        row: dict[str, Any] | None,
        *,
        running_conflict: bool = False,
    ) -> None:
        self.conn = conn
        self.row = row
        self.running_conflict = running_conflict
        self._finished = conn is None

    @property
    def active(self) -> bool:
        return self.row is not None

    def close(self, *, state: str, reason: str) -> dict[str, Any] | None:
        if state not in _CLOSED_STATES:
            raise ValueError(f"invalid refinement close state: {state}")
        if self.conn is None or self.row is None:
            self.release()
            return None
        now = _utc_now()
        self.conn.execute(
            """UPDATE session_worktrees
               SET state = ?, closed_at = ?, updated_at = ?, close_reason = ?
               WHERE id = ? AND state = 'active'""",
            (state, now, now, reason[:500], self.row["id"]),
        )
        updated = self.conn.execute(
            "SELECT * FROM session_worktrees WHERE id = ?", (self.row["id"],)
        ).fetchone()
        self.conn.commit()
        self._finished = True
        result = dict(updated) if updated is not None else None
        self.conn.close()
        self.conn = None
        return result

    def release(self) -> None:
        if self.conn is None:
            self._finished = True
            return
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
        finally:
            self.conn.close()
            self.conn = None
            self._finished = True


class RefinementStore:
    def __init__(self, settings: Any, *, db_path: str | Path | None = None) -> None:
        self.settings = settings
        self.db_path = (
            Path(db_path)
            if db_path is not None
            else Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
        )
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS session_worktrees (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        operator_id TEXT NOT NULL,
                        source_chat_id TEXT NOT NULL,
                        worktree_path TEXT NOT NULL,
                        owner_runtime_job_id TEXT NOT NULL,
                        root_queue_job_id TEXT,
                        latest_queue_job_id TEXT,
                        latest_runtime_job_id TEXT NOT NULL,
                        turn_count INTEGER NOT NULL DEFAULT 1,
                        state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        closed_at TEXT,
                        close_reason TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_worktrees_session_state
                        ON session_worktrees(session_id, state, updated_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_worktrees_one_active
                        ON session_worktrees(session_id) WHERE state = 'active';
                    CREATE INDEX IF NOT EXISTS idx_session_worktrees_path_state
                        ON session_worktrees(worktree_path, state);
                    """
                )
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def active(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM session_worktrees WHERE session_id = ? AND state = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def active_for_worktree(self, worktree_path: str | Path) -> dict[str, Any] | None:
        resolved = str(Path(worktree_path).resolve())
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM session_worktrees WHERE worktree_path = ? AND state = 'active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (resolved,),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def bind_new(
        self,
        *,
        session_id: str,
        channel: str,
        operator_id: str,
        source_chat_id: str,
        worktree_path: str | Path,
        runtime_job_id: str,
        queue_job_id: str | None,
    ) -> dict[str, Any]:
        """Bind a newly-created worktree to a session after creation succeeds."""
        chain_id = uuid.uuid4().hex
        path = str(Path(worktree_path).resolve())
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM session_worktrees WHERE session_id = ? AND state = 'active' LIMIT 1",
                (session_id,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                raise RuntimeError("Session already has an active refinement worktree.")
            conn.execute(
                """INSERT INTO session_worktrees (
                       id, session_id, channel, operator_id, source_chat_id,
                       worktree_path, owner_runtime_job_id, root_queue_job_id,
                       latest_queue_job_id, latest_runtime_job_id, turn_count,
                       state, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)""",
                (
                    chain_id, session_id, channel, operator_id, source_chat_id,
                    path, runtime_job_id, queue_job_id, queue_job_id,
                    runtime_job_id, now, now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM session_worktrees WHERE id = ?", (chain_id,)).fetchone()
            assert row is not None
            return dict(row)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def continue_active(
        self,
        *,
        session_id: str,
        chain_id: str,
        expected_worktree_path: str | Path,
        runtime_job_id: str,
        queue_job_id: str | None,
    ) -> dict[str, Any]:
        """Atomically claim the current active chain for the next queued turn."""
        path = str(Path(expected_worktree_path).resolve())
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM session_worktrees WHERE id = ? AND session_id = ? AND state = 'active'",
                (chain_id, session_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RuntimeError("Active refinement chain is no longer available.")
            if str(Path(row["worktree_path"]).resolve()) != path:
                conn.rollback()
                raise RuntimeError("Active refinement worktree changed while the job was queued.")
            conn.execute(
                """UPDATE session_worktrees
                   SET latest_queue_job_id = ?, latest_runtime_job_id = ?,
                       turn_count = turn_count + 1, updated_at = ?
                   WHERE id = ? AND state = 'active'""",
                (queue_job_id, runtime_job_id, now, chain_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM session_worktrees WHERE id = ?", (chain_id,)).fetchone()
            assert updated is not None
            return dict(updated)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def begin_mutation(self, worktree_path: str | Path) -> RefinementMutation:
        """Acquire a DB guard for Apply/Discard or report an active writer."""
        path = str(Path(worktree_path).resolve())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM session_worktrees WHERE worktree_path = ? AND state = 'active' LIMIT 1",
                (path,),
            ).fetchone()
            if row is None:
                conn.rollback()
                conn.close()
                return RefinementMutation(None, None)
            columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(queued_jobs)").fetchall()}
            if "session_id" in columns:
                running = conn.execute(
                    "SELECT 1 FROM queued_jobs WHERE session_id = ? AND state = 'running' LIMIT 1",
                    (row["session_id"],),
                ).fetchone()
                if running is not None:
                    conn.rollback()
                    conn.close()
                    return RefinementMutation(None, dict(row), running_conflict=True)
            return RefinementMutation(conn, dict(row))
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
            raise

    def close_active_for_worktree(
        self,
        worktree_path: str | Path,
        *,
        state: str,
        reason: str,
    ) -> dict[str, Any] | None:
        if state not in _CLOSED_STATES:
            raise ValueError(f"invalid refinement close state: {state}")
        path = str(Path(worktree_path).resolve())
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM session_worktrees WHERE worktree_path = ? AND state = 'active' LIMIT 1",
                (path,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                """UPDATE session_worktrees
                   SET state = ?, closed_at = ?, updated_at = ?, close_reason = ?
                   WHERE id = ? AND state = 'active'""",
                (state, now, now, reason[:500], row["id"]),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM session_worktrees WHERE id = ?", (row["id"],)).fetchone()
            return self._row(updated)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def mark_stale(self, chain_id: str, reason: str) -> dict[str, Any] | None:
        now = _utc_now()
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """UPDATE session_worktrees
                       SET state = 'stale', closed_at = ?, updated_at = ?, close_reason = ?
                       WHERE id = ? AND state = 'active'""",
                    (now, now, reason[:500], chain_id),
                )
                if cursor.rowcount == 0:
                    return None
            row = conn.execute("SELECT * FROM session_worktrees WHERE id = ?", (chain_id,)).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def pending_or_running(self, session_id: str, *, exclude_job_id: str | None = None) -> bool:
        """Return whether this stable session has queued/running execution intent."""
        conn = self._connect()
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(queued_jobs)").fetchall()}
            if "session_id" not in columns:
                return False
            sql = "SELECT 1 FROM queued_jobs WHERE session_id = ? AND state IN ('queued','running')"
            params: list[Any] = [session_id]
            if exclude_job_id:
                sql += " AND id != ?"
                params.append(exclude_job_id)
            sql += " LIMIT 1"
            return conn.execute(sql, params).fetchone() is not None
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    @staticmethod
    def _public_summary(active: dict[str, Any]) -> dict[str, Any]:
        """UI-safe fields only: never expose a host filesystem worktree path."""
        return {
            "chain_id": active["id"],
            "state": active["state"],
            "turn_count": int(active["turn_count"] or 0),
            "root_queue_job_id": active["root_queue_job_id"],
            "latest_queue_job_id": active["latest_queue_job_id"],
            "latest_runtime_job_id": active["latest_runtime_job_id"],
            "updated_at": active["updated_at"],
        }

    def session_summary(self, session_id: str) -> dict[str, Any] | None:
        active = self.active(session_id)
        return self._public_summary(active) if active else None

    def active_summaries(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read the visible sessions' authoritative active chains in one query.

        The control plane polls sessions frequently. One bounded read avoids
        opening and migrating SQLite once per session on every refresh.
        """
        keys = list(dict.fromkeys(value for value in session_ids if isinstance(value, str) and value))
        if not keys:
            return {}
        result: dict[str, dict[str, Any]] = {}
        conn = self._connect()
        try:
            # SQLite installations with a 999-variable limit are supported.
            for offset in range(0, len(keys), 400):
                batch = keys[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM session_worktrees WHERE state = 'active' "
                    f"AND session_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    record = dict(row)
                    result[record["session_id"]] = self._public_summary(record)
            return result
        finally:
            conn.close()


_EXPLANATION_RE = re.compile(
    r"(?:为什么|为啥|怎么回事|解释|原因|风险|分别干嘛|做了什么|是什么|什么是|\?|？|"
    r"\b(?:why|explain|what\s+(?:is|are|did)|how\s+(?:does|did)|risk|reason)\b)",
    re.IGNORECASE,
)
_REFINEMENT_RE = re.compile(
    r"(?:还是|再|继续|一点|一些|太(?:挤|宽|窄|大|小|快|慢|亮|暗|圆)|不对|往(?:上|下|左|右)|"
    r"颜色|按钮|动画|间距|空白|圆角|宽|窄|淡|深|暗|亮|挤|移动|放大|缩小|"
    r"\b(?:continue|still|again|a\s+(?:little|bit)|less|more|darker|lighter|wider|narrower|"
    r"rounded|move\s+it|too\s+(?:wide|narrow|dark|light|big|small|fast|slow|crowded))\b)",
    re.IGNORECASE,
)


def is_refinement_feedback(text: str) -> bool:
    value = (text or "").strip()
    if not value or len(value) > 500:
        return False
    if _EXPLANATION_RE.search(value):
        return False
    return bool(_REFINEMENT_RE.search(value))


def stable_session_id(channel: str, chat_id: str, operator_id: str) -> str:
    return session_identity(channel, chat_id, operator_id)


def should_route_refinement(settings: Any, msg: Any) -> bool:
    if not is_refinement_feedback(getattr(msg, "text", "")):
        return False
    session_id = stable_session_id(msg.channel, msg.chat_id, msg.operator_id)
    store = RefinementStore(settings)
    return store.active(session_id) is not None or store.pending_or_running(session_id)
