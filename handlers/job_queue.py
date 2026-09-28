"""handlers/job_queue.py — persistent FIFO queue for Codex jobs.

P4.4: Makes the Codex job queue survive bot restarts, deploy restarts, and VPS reboots
by storing queue items in SQLite under codex_memory_root/state/job_queue.sqlite3.

Multi-turn refinement adds only persistent execution intent here. Queue jobs
remain independent; a session-scoped refinement chain is resolved when the
runtime job actually starts, so a queued follow-up can wait for the previous
turn to create its worktree.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Any

from redaction import redact_text, truncate
from refinement_store import RefinementStore, stable_session_id

if TYPE_CHECKING:
    from channel.types import InboundMessage, OutboundPort
    from runner import CodexRunner
    from config import Settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUEUE_LENGTH = 10


class QueueJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass
class QueuedJob:
    """A job waiting in the queue."""
    id: str
    mode: str
    prompt: str
    channel: str
    chat_id: str
    operator_id: str
    original_text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: QueueJobState = QueueJobState.QUEUED
    position: int = 0
    # Stable cross-channel identity and persisted expectation that this job
    # must continue an existing/pending refinement chain rather than silently
    # falling back to a fresh worktree.
    session_id: str = ""
    refinement_intent: bool = False

    _msg: "InboundMessage | None" = field(default=None, repr=False)
    _port: "OutboundPort | None" = field(default=None, repr=False)
    _runner: "CodexRunner | None" = field(default=None, repr=False)

    @property
    def prompt_preview(self) -> str:
        return truncate(redact_text(self.prompt), 200)

    @property
    def original_text_preview(self) -> str:
        return truncate(redact_text(self.original_text), 100)


class JobQueue:
    """Persistent FIFO queue for Codex jobs using SQLite."""

    def __init__(self, max_length: int = DEFAULT_MAX_QUEUE_LENGTH) -> None:
        self._max_length = max_length
        self._lock_obj: asyncio.Lock | None = None
        self._paused: bool = False
        self._counter: int = 0
        self._start_callback: Callable[[QueuedJob], Awaitable[None]] | None = None
        self._settings: "Settings | None" = None
        self._runner: "CodexRunner | None" = None
        self._memory_references: dict[str, dict[str, Any]] = {}

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def queue_length(self) -> int:
        conn = self._get_conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'"
            ).fetchone()[0])
        finally:
            conn.close()

    @property
    def has_running_job(self) -> bool:
        conn = self._get_conn()
        try:
            return bool(conn.execute(
                "SELECT 1 FROM queued_jobs WHERE state = 'running' LIMIT 1"
            ).fetchone())
        finally:
            conn.close()

    def _emit(
        self,
        kind: str,
        job_id: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        if self._settings is None:
            return
        try:
            from agent_events import emit_event
            emit_event(self._settings, kind, job_id, payload or {}, session_id=session_id)
        except Exception:
            logger.exception("Could not persist agent event %s for %s", kind, job_id)

    def set_start_callback(self, callback: Callable[[QueuedJob], Awaitable[None]]) -> None:
        self._start_callback = callback

    def configure(self, settings: "Settings", runner: "CodexRunner", *, recover: bool = True) -> None:
        self._settings = settings
        self._runner = runner
        if hasattr(settings, "conveyor_max_pending_jobs"):
            self._max_length = settings.conveyor_max_pending_jobs
        # Ensure the refinement table shares this exact DB before recovery.
        RefinementStore(settings)
        self.recover_and_load(mark_interrupted=recover)

    def _db_path(self) -> Path:
        if self._settings and hasattr(self._settings, "codex_memory_root"):
            root = Path(self._settings.codex_memory_root)
        else:
            try:
                from config import load_settings
                root = Path(load_settings().codex_memory_root)
            except Exception:
                root = Path(os.getenv("CODEX_MEMORY_ROOT", "~/.codex")).expanduser().resolve()
        return root / "state" / "job_queue.sqlite3"

    def _get_conn(self) -> sqlite3.Connection:
        db_path = self._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            db_path.parent.chmod(0o700)
        except OSError:
            pass
        exists = db_path.exists()
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        if not exists:
            try:
                db_path.chmod(0o600)
            except OSError:
                pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        self._init_db(conn)
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
        """Additive/idempotent migration; old queue DBs remain readable."""
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queued_jobs (
                    id TEXT PRIMARY KEY,
                    operator_id TEXT,
                    channel TEXT,
                    chat_id TEXT,
                    mode TEXT,
                    prompt TEXT,
                    state TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    position INTEGER,
                    metadata_json TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(queued_jobs)")}
            if "session_id" not in columns:
                conn.execute("ALTER TABLE queued_jobs ADD COLUMN session_id TEXT")
            if "refinement_intent" not in columns:
                conn.execute(
                    "ALTER TABLE queued_jobs ADD COLUMN refinement_intent INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queued_jobs_session_state "
                "ON queued_jobs(session_id, state, created_at)"
            )

    def recover_and_load(self, *, mark_interrupted: bool = True) -> None:
        conn = self._get_conn()
        now_str = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                if mark_interrupted:
                    conn.execute(
                        "UPDATE queued_jobs SET state = 'interrupted', finished_at = ?, position = 0 "
                        "WHERE state = 'running'",
                        (now_str,),
                    )
                row = conn.execute(
                    "SELECT value FROM queue_metadata WHERE key = 'paused'"
                ).fetchone()
                self._paused = bool(row and row[0] == "true")
                row = conn.execute(
                    "SELECT value FROM queue_metadata WHERE key = 'counter'"
                ).fetchone()
                self._counter = int(row[0]) if row else 0
        finally:
            conn.close()
        logger.info("JobQueue recovered and loaded (paused=%s, counter=%d)", self._paused, self._counter)

    def _next_id(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT value FROM queue_metadata WHERE key = 'counter'").fetchone()
        counter = int(row[0]) + 1 if row else 1
        conn.execute(
            "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('counter', ?)",
            (str(counter),),
        )
        self._counter = counter
        return f"q{counter}"

    def _recalculate_positions(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id FROM queued_jobs WHERE state = 'queued' ORDER BY position ASC, created_at ASC"
        ).fetchall()
        for i, row in enumerate(rows):
            conn.execute("UPDATE queued_jobs SET position = ? WHERE id = ?", (i + 1, row[0]))

    def _row_to_job(
        self,
        row: sqlite3.Row,
        msg: "InboundMessage | None" = None,
        port: "OutboundPort | None" = None,
        runner: "CodexRunner | None" = None,
    ) -> QueuedJob:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except Exception:
            metadata = {}
        keys = set(row.keys())
        return QueuedJob(
            id=row["id"],
            mode=row["mode"],
            prompt=row["prompt"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            operator_id=row["operator_id"],
            original_text=metadata.get("original_text", row["prompt"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            state=QueueJobState(row["state"]),
            position=row["position"],
            session_id=str(row["session_id"] or "") if "session_id" in keys else "",
            refinement_intent=bool(row["refinement_intent"]) if "refinement_intent" in keys else False,
            _msg=msg,
            _port=port,
            _runner=runner,
        )

    async def enqueue(
        self,
        mode: str,
        prompt: str,
        msg: "InboundMessage",
        port: "OutboundPort",
        runner: "CodexRunner",
        original_text: str | None = None,
    ) -> tuple[bool, str, QueuedJob | None]:
        """Add a queue job and persist whether it expects chain continuation."""
        session_id = stable_session_id(msg.channel, msg.chat_id, msg.operator_id)
        settings = self._settings or getattr(runner, "settings", None)
        if settings is not None:
            RefinementStore(settings)

        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                count = int(conn.execute(
                    "SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'"
                ).fetchone()[0])
                if count >= self._max_length:
                    conn.rollback()
                    return False, f"队列已满（最多 {self._max_length} 个任务）", None

                active = conn.execute(
                    "SELECT 1 FROM session_worktrees WHERE session_id = ? AND state = 'active' LIMIT 1",
                    (session_id,),
                ).fetchone()
                earlier = conn.execute(
                    "SELECT 1 FROM queued_jobs WHERE session_id = ? AND state IN ('queued','running') LIMIT 1",
                    (session_id,),
                ).fetchone()
                refinement_intent = bool(active or earlier)

                job_id = self._next_id(conn)
                now_str = datetime.now(timezone.utc).isoformat()
                metadata_json = json.dumps({
                    "original_text": original_text or msg.text,
                    "session_id": session_id,
                    "refinement_intent": refinement_intent,
                })
                conn.execute(
                    """INSERT INTO queued_jobs (
                           id, operator_id, channel, chat_id, mode, prompt, state,
                           created_at, updated_at, position, metadata_json,
                           session_id, refinement_intent
                       ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
                    (
                        job_id, msg.operator_id, msg.channel, msg.chat_id, mode, prompt,
                        now_str, now_str, count + 1, metadata_json,
                        session_id, 1 if refinement_intent else 0,
                    ),
                )
                self._recalculate_positions(conn)
                row = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (job_id,)).fetchone()
                conn.commit()
                assert row is not None
                queued_job = self._row_to_job(row, msg, port, runner)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

            # Once a message reaches this function it is an execution job,
            # including Web Ask messages conservatively escalated by dispatch.
            if hasattr(port, "is_codex"):
                try:
                    port.is_codex = True
                except Exception:
                    pass
            self._memory_references[job_id] = {"msg": msg, "port": port, "runner": runner}
            logger.info(
                "Job %s queued at position %d (channel=%s, operator=%s, refinement=%s)",
                job_id, queued_job.position, msg.channel, msg.operator_id, refinement_intent,
            )
            self._emit("task.created", job_id, {
                "mode": mode,
                "channel": msg.channel,
                "text": original_text or msg.text,
                "refinement_intent": refinement_intent,
            }, session_id=msg.chat_id)
            self._emit("task.queued", job_id, {
                "position": queued_job.position,
                "refinement_intent": refinement_intent,
            }, session_id=msg.chat_id)
            return True, (
                f"⏳ 任务已排队\n"
                f"队列位置: {queued_job.position}/{count + 1}\n"
                f"队列 ID: {job_id}\n"
                f"提示: {queued_job.prompt_preview}"
            ), queued_job

    async def dequeue(self, *, require_idle: bool = False) -> QueuedJob | None:
        async with self._lock:
            if self._paused:
                return None
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if require_idle and conn.execute(
                        "SELECT 1 FROM queued_jobs WHERE state = 'running' LIMIT 1"
                    ).fetchone():
                        conn.rollback()
                        return None
                    row = conn.execute(
                        "SELECT * FROM queued_jobs WHERE state = 'queued' "
                        "ORDER BY position ASC, created_at ASC LIMIT 1"
                    ).fetchone()
                    if row is None:
                        conn.rollback()
                        return None
                    job_id = row["id"]
                    now_str = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE queued_jobs SET state = 'running', started_at = ?, updated_at = ?, "
                        "position = 0 WHERE id = ?",
                        (now_str, now_str, job_id),
                    )
                    self._recalculate_positions(conn)
                    updated_row = conn.execute(
                        "SELECT * FROM queued_jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            finally:
                conn.close()

            refs = self._memory_references.get(job_id, {})
            job = self._row_to_job(
                updated_row,
                msg=refs.get("msg"),
                port=refs.get("port"),
                runner=refs.get("runner") or self._runner,
            )
            logger.info("Job %s dequeued (remaining queued count: %d)", job.id, self._get_queued_count())
            self._emit("task.started", job.id, {
                "mode": job.mode,
                "refinement_intent": job.refinement_intent,
            }, session_id=job.chat_id)
            return job

    def _get_queued_count(self, conn: sqlite3.Connection | None = None) -> int:
        if conn is not None:
            return int(conn.execute(
                "SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'"
            ).fetchone()[0])
        conn = self._get_conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'"
            ).fetchone()[0])
        finally:
            conn.close()

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        async with self._lock:
            conn = self._get_conn()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT * FROM queued_jobs WHERE id = ? AND state = 'queued'", (job_id,)
                    ).fetchone()
                    if row is None:
                        return False, f"未找到队列任务 {job_id}"
                    now_str = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE queued_jobs SET state = 'cancelled', finished_at = ?, updated_at = ?, "
                        "position = 0 WHERE id = ?",
                        (now_str, now_str, job_id),
                    )
                    self._recalculate_positions(conn)
            finally:
                conn.close()
            self._memory_references.pop(job_id, None)
            logger.info("Job %s cancelled from queue", job_id)
            self._emit("task.cancelled", job_id, {})
            return True, f"已取消队列任务 {job_id}"

    def bind_runtime_job(self, queue_job_id: str, runtime_job: Any) -> None:
        """Bind queue/runtime identity and resolve refinement reuse at start time.

        The first call happens immediately after ``runner.start`` and before the
        scheduled runner task creates a worktree. A second call from the
        worktree layer persists the actual path/chain after successful binding.
        """
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (queue_job_id,)).fetchone()
            if row is None:
                return
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except Exception:
                metadata = {}

            session_id = str(row["session_id"] or "")
            refinement_intent = bool(row["refinement_intent"])
            runtime_job.external_id = queue_job_id
            runtime_job.refinement_session_id = session_id
            runtime_job.refinement_intent = refinement_intent
            runtime_job.refinement_channel = str(row["channel"] or "")
            runtime_job.refinement_operator_id = str(row["operator_id"] or "")
            runtime_job.refinement_source_chat_id = str(row["chat_id"] or "")
            runtime_job.refinement_queue_job_id = queue_job_id

            settings = self._settings or getattr(self._runner, "settings", None)
            if settings is not None and session_id and not getattr(runtime_job, "refinement_bound", False):
                active = RefinementStore(settings).active(session_id)
                if active is not None:
                    runtime_job.reuse_worktree_path = Path(active["worktree_path"])
                    runtime_job.refinement_chain_id = str(active["id"])
                    runtime_job.refinement_owner_job_id = str(active["owner_runtime_job_id"])
                    runtime_job.refinement_root_queue_job_id = active["root_queue_job_id"]
                    runtime_job.refinement_parent_queue_job_id = active["latest_queue_job_id"]
                    runtime_job.refinement_turn = int(active["turn_count"] or 0) + 1
                    runtime_job.reused_worktree = True
                elif refinement_intent:
                    # This was explicitly queued as a continuation. If Apply,
                    # Discard, deletion, or failed creation removed the chain
                    # while it waited, never silently convert it to a new task.
                    runtime_job.refinement_resolution_error = (
                        "Active refinement chain is no longer available; "
                        "the queued follow-up was not run in a new worktree."
                    )
                else:
                    runtime_job.reused_worktree = False
                    runtime_job.refinement_turn = 1

            metadata["runtime_job_id"] = str(getattr(runtime_job, "id", ""))
            metadata["session_id"] = session_id
            metadata["refinement_intent"] = refinement_intent
            for key in (
                "refinement_chain_id", "refinement_owner_job_id",
                "refinement_root_queue_job_id", "refinement_parent_queue_job_id",
                "refinement_turn", "reused_worktree",
            ):
                value = getattr(runtime_job, key, None)
                if value is not None:
                    metadata[key] = value
            worktree = getattr(runtime_job, "worktree_path", None)
            if worktree:
                metadata["worktree_path"] = str(worktree)
            with conn:
                conn.execute(
                    "UPDATE queued_jobs SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata), datetime.now(timezone.utc).isoformat(), queue_job_id),
                )
        finally:
            conn.close()

    def list_jobs(self, limit: int = 100, *, session_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            sql = "SELECT * FROM queued_jobs"
            params: list[Any] = []
            if session_id is not None:
                # This API historically takes source chat id; keep that behavior
                # for WebControl and existing clients.
                sql += " WHERE chat_id = ?"
                params.append(session_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(min(max(1, int(limit)), 500))
            rows = conn.execute(sql, params).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = {key: row[key] for key in row.keys() if key != "prompt"}
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except Exception:
                    metadata = {}
                item["metadata"] = metadata
                item.pop("metadata_json", None)
                item["refinement_intent"] = bool(item.get("refinement_intent"))
                item["prompt_preview"] = truncate(redact_text(row["prompt"] or ""), 500)
                result.append(item)
            return result
        finally:
            conn.close()

    def job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_jobs(500) if item["id"] == job_id), None)

    async def clear(self) -> int:
        async with self._lock:
            conn = self._get_conn()
            now_str = datetime.now(timezone.utc).isoformat()
            try:
                with conn:
                    queued_ids = [r[0] for r in conn.execute(
                        "SELECT id FROM queued_jobs WHERE state = 'queued'"
                    ).fetchall()]
                    if not queued_ids:
                        return 0
                    conn.execute(
                        "UPDATE queued_jobs SET state = 'cancelled', finished_at = ?, updated_at = ?, "
                        "position = 0 WHERE state = 'queued'",
                        (now_str, now_str),
                    )
            finally:
                conn.close()
            for job_id in queued_ids:
                self._memory_references.pop(job_id, None)
            logger.info("Queue cleared (%d jobs removed)", len(queued_ids))
            return len(queued_ids)

    async def pause(self) -> None:
        async with self._lock:
            self._paused = True
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('paused', 'true')"
                    )
            finally:
                conn.close()
            logger.info("Queue paused")

    async def resume(self) -> None:
        async with self._lock:
            self._paused = False
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('paused', 'false')"
                    )
            finally:
                conn.close()
            logger.info("Queue resumed")

    async def get_queue_status(self) -> str:
        async with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("SELECT state, COUNT(*) FROM queued_jobs GROUP BY state").fetchall()
                counts = {state: 0 for state in (
                    "queued", "running", "interrupted", "completed", "failed", "cancelled"
                )}
                for row in rows:
                    if row[0] in counts:
                        counts[row[0]] = row[1]
                queued_rows = conn.execute(
                    "SELECT * FROM queued_jobs WHERE state = 'queued' "
                    "ORDER BY position ASC, created_at ASC"
                ).fetchall()
            finally:
                conn.close()

            lines = [
                "📋 任务队列状态",
                f"运行状态: {'已暂停' if self._paused else '运行中'}",
                f"排队中 (queued): {counts['queued']}/{self._max_length}",
                f"正在运行 (running): {counts['running']}",
                f"已中断 (interrupted): {counts['interrupted']}",
                f"已完成 (completed): {counts['completed']}",
                f"已失败 (failed): {counts['failed']}",
                f"已取消 (cancelled): {counts['cancelled']}",
            ]
            if queued_rows:
                lines.extend(["", "排队中的任务:"])
                for row in queued_rows:
                    job = self._row_to_job(row)
                    lines.append(
                        f"  #{job.position} [{job.id}] {job.mode}\n"
                        f"    提示: {job.prompt_preview}\n"
                        f"    来源: {job.channel}/{job.chat_id[:8]}...\n"
                        f"    创建: {job.created_at.strftime('%H:%M:%S')}"
                    )
            else:
                lines.extend(["", "📋 任务队列为空"])
            return "\n".join(lines)

    async def get_job(self, job_id: str) -> QueuedJob | None:
        async with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    return None
            finally:
                conn.close()
            refs = self._memory_references.get(job_id, {})
            return self._row_to_job(
                row,
                msg=refs.get("msg"),
                port=refs.get("port"),
                runner=refs.get("runner") or self._runner,
            )

    async def mark_running_failed(self, error_message: str) -> None:
        conn = self._get_conn()
        now_str = datetime.now(timezone.utc).isoformat()
        redacted_err = redact_text(error_message)
        running_id: str | None = None
        try:
            async with self._lock:
                with conn:
                    row = conn.execute(
                        "SELECT id FROM queued_jobs WHERE state = 'running' LIMIT 1"
                    ).fetchone()
                    if row:
                        running_id = str(row[0])
                        conn.execute(
                            "UPDATE queued_jobs SET state = ?, finished_at = ?, updated_at = ?, error = ? "
                            "WHERE id = ?",
                            (QueueJobState.FAILED.value, now_str, now_str, redacted_err, running_id),
                        )
                        self._memory_references.pop(running_id, None)
                        logger.info("Marked running job %s as failed (start-failed)", running_id)
        finally:
            conn.close()
        if running_id:
            self._emit("task.failed", running_id, {"error": redacted_err})
        await self._start_next_if_owned(now_str)

    async def on_job_completed(
        self,
        job_id: str | None = None,
        *,
        queue_job_id: str | None = None,
        final_state: str | None = None,
        error: str | None = None,
    ) -> None:
        conn = self._get_conn()
        now_str = datetime.now(timezone.utc).isoformat()
        error_msg = error
        state = QueueJobState.FAILED if final_state == "failed" else (
            QueueJobState.CANCELLED if final_state == "cancelled" else QueueJobState.COMPLETED
        )
        current_job = getattr(self._runner, "current_job", None) if self._runner else None
        if current_job and (job_id is None or str(getattr(current_job, "id", "")) == job_id):
            current_state = getattr(getattr(current_job, "state", None), "value", "")
            if getattr(current_job, "error", ""):
                error_msg = current_job.error
                state = QueueJobState.FAILED
            elif current_state == "failed":
                state = QueueJobState.FAILED

        running_id: str | None = None
        try:
            with conn:
                if queue_job_id:
                    row = conn.execute(
                        "SELECT id FROM queued_jobs WHERE id = ? AND state = 'running'",
                        (queue_job_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id FROM queued_jobs WHERE state = 'running' LIMIT 1"
                    ).fetchone()
                if row:
                    running_id = str(row[0])
                    conn.execute(
                        "UPDATE queued_jobs SET state = ?, finished_at = ?, updated_at = ?, error = ? "
                        "WHERE id = ?",
                        (state.value, now_str, now_str, error_msg, running_id),
                    )
                    self._memory_references.pop(running_id, None)
        finally:
            conn.close()
        if running_id:
            terminal_kind = {
                QueueJobState.FAILED: "task.failed",
                QueueJobState.CANCELLED: "task.cancelled",
            }.get(state, "task.completed")
            self._emit(
                terminal_kind,
                running_id,
                {"error": redact_text(error_msg or "")} if error_msg else {},
            )
        await self._start_next_if_owned(now_str)

    async def _start_next_if_owned(self, now_str: str) -> None:
        if self._paused:
            logger.debug("Queue paused, not starting next job")
            return
        if self._start_callback is None:
            logger.debug("No start callback set; leaving queued jobs unclaimed")
            return
        next_job = await self.dequeue(require_idle=True)
        if next_job is None:
            return
        logger.info("Starting queued job %s", next_job.id)
        try:
            await self._start_callback(next_job)
        except Exception as exc:
            logger.exception("Failed to start queued job %s", next_job.id)
            conn = self._get_conn()
            try:
                with conn:
                    conn.execute(
                        "UPDATE queued_jobs SET state = 'failed', finished_at = ?, updated_at = ?, error = ? "
                        "WHERE id = ?",
                        (now_str, now_str, redact_text(str(exc)), next_job.id),
                    )
            finally:
                conn.close()


_job_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _job_queue
    if _job_queue is None:
        _job_queue = JobQueue()
    return _job_queue


def reset_job_queue() -> None:
    global _job_queue
    _job_queue = None
