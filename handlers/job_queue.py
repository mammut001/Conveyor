"""handlers/job_queue.py — persistent FIFO queue for Codex jobs.

P4.4: Makes the Codex job queue survive bot restarts, deploy restarts, and VPS reboots
by storing queue items in SQLite under codex_memory_root/state/job_queue.sqlite3.
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

if TYPE_CHECKING:
    from channel.types import InboundMessage, OutboundPort
    from runner import CodexRunner, JobMode
    from config import Settings

logger = logging.getLogger(__name__)

# Default max queue length
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
    mode: str  # JobMode value (run/fix)
    prompt: str
    channel: str
    chat_id: str
    operator_id: str
    original_text: str  # Original user text (for display)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: QueueJobState = QueueJobState.QUEUED
    position: int = 0  # Position in queue (1-indexed)

    # These are set when the job is dequeued and started
    _msg: "InboundMessage | None" = field(default=None, repr=False)
    _port: "OutboundPort | None" = field(default=None, repr=False)
    _runner: "CodexRunner | None" = field(default=None, repr=False)

    @property
    def prompt_preview(self) -> str:
        """Truncated, redacted prompt for display."""
        return truncate(redact_text(self.prompt), 200)

    @property
    def original_text_preview(self) -> str:
        """Truncated, redacted original text for display."""
        return truncate(redact_text(self.original_text), 100)


class JobQueue:
    """Persistent FIFO queue for Codex jobs using SQLite."""

    def __init__(self, max_length: int = DEFAULT_MAX_QUEUE_LENGTH) -> None:
        self._max_length = max_length
        self._lock_obj: asyncio.Lock | None = None
        self._paused: bool = False
        self._counter: int = 0
        # Callback to start a job (set by the integration layer)
        self._start_callback: Callable[[QueuedJob], Awaitable[None]] | None = None
        
        self._settings: "Settings | None" = None
        self._runner: "CodexRunner | None" = None
        
        # In-memory references to preserve live objects (msg, port, runner) for active sessions
        self._memory_references: dict[str, dict[str, Any]] = {}

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    @property
    def is_paused(self) -> bool:
        """Whether automatic dequeue is paused."""
        return self._paused

    @property
    def queue_length(self) -> int:
        """Number of active queued jobs."""
        conn = self._get_conn()
        with conn:
            cur = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'")
            return cur.fetchone()[0]

    def set_start_callback(self, callback: Callable[[QueuedJob], Awaitable[None]]) -> None:
        """Set the callback to start a queued job."""
        self._start_callback = callback

    def configure(self, settings: "Settings", runner: "CodexRunner") -> None:
        """Configure settings and runner, and load/recover database."""
        self._settings = settings
        self._runner = runner
        self.recover_and_load()

    def _db_path(self) -> Path:
        if self._settings and hasattr(self._settings, "codex_memory_root"):
            root = Path(self._settings.codex_memory_root)
        else:
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
        conn = sqlite3.connect(str(db_path))
        if not exists:
            try:
                db_path.chmod(0o600)
            except OSError:
                pass
        conn.row_factory = sqlite3.Row
        self._init_db(conn)
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
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

    def recover_and_load(self) -> None:
        """Recover interrupted running jobs and reload paused state and counter."""
        conn = self._get_conn()
        now_str = datetime.now(timezone.utc).isoformat()
        with conn:
            # 1. Update running jobs to interrupted on startup
            conn.execute(
                "UPDATE queued_jobs SET state = 'interrupted', finished_at = ?, position = 0 WHERE state = 'running'",
                (now_str,)
            )
            
            # 2. Load paused state
            cur = conn.execute("SELECT value FROM queue_metadata WHERE key = 'paused'")
            row = cur.fetchone()
            if row:
                self._paused = (row[0] == 'true')
            else:
                self._paused = False
                
            # 3. Load counter
            cur = conn.execute("SELECT value FROM queue_metadata WHERE key = 'counter'")
            row = cur.fetchone()
            if row:
                self._counter = int(row[0])
            else:
                self._counter = 0
        logger.info("JobQueue recovered and loaded (paused=%s, counter=%d)", self._paused, self._counter)

    def _next_id(self, conn: sqlite3.Connection) -> str:
        cur = conn.execute("SELECT value FROM queue_metadata WHERE key = 'counter'")
        row = cur.fetchone()
        if row:
            counter = int(row[0]) + 1
        else:
            counter = 1
        conn.execute(
            "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('counter', ?)",
            (str(counter),)
        )
        self._counter = counter
        return f"q{counter}"

    def _recalculate_positions(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("SELECT id FROM queued_jobs WHERE state = 'queued' ORDER BY position ASC, created_at ASC")
        rows = cur.fetchall()
        for i, row in enumerate(rows):
            conn.execute(
                "UPDATE queued_jobs SET position = ? WHERE id = ?",
                (i + 1, row[0])
            )

    def _row_to_job(self, row: sqlite3.Row, msg: "InboundMessage | None" = None, port: "OutboundPort | None" = None, runner: "CodexRunner | None" = None) -> QueuedJob:
        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except Exception:
                pass
        original_text = metadata.get("original_text", row["prompt"])
        created_at = datetime.fromisoformat(row["created_at"])
        
        return QueuedJob(
            id=row["id"],
            mode=row["mode"],
            prompt=row["prompt"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            operator_id=row["operator_id"],
            original_text=original_text,
            created_at=created_at,
            state=QueueJobState(row["state"]),
            position=row["position"],
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
        """Add a job to the queue."""
        async with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'")
                count = cur.fetchone()[0]
                if count >= self._max_length:
                    return False, f"队列已满（最多 {self._max_length} 个任务）", None

                job_id = self._next_id(conn)
                now_str = datetime.now(timezone.utc).isoformat()
                metadata_json = json.dumps({"original_text": original_text or msg.text})
                
                conn.execute(
                    """
                    INSERT INTO queued_jobs (
                        id, operator_id, channel, chat_id, mode, prompt, state, created_at, updated_at, position, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (
                        job_id, msg.operator_id, msg.channel, msg.chat_id, mode, prompt,
                        now_str, now_str, count + 1, metadata_json
                    )
                )
                self._recalculate_positions(conn)
                
                cur = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (job_id,))
                row = cur.fetchone()
                queued_job = self._row_to_job(row, msg, port, runner)

            # Store memory references for live session processing
            self._memory_references[job_id] = {"msg": msg, "port": port, "runner": runner}
            logger.info(
                "Job %s queued at position %d (channel=%s, operator=%s)",
                job_id, queued_job.position, msg.channel, msg.operator_id,
            )
            return True, (
                f"⏳ 任务已排队\n"
                f"队列位置: {queued_job.position}/{count + 1}\n"
                f"队列 ID: {job_id}\n"
                f"提示: {queued_job.prompt_preview}"
            ), queued_job

    async def dequeue(self) -> QueuedJob | None:
        """Remove and return the next job from the queue."""
        async with self._lock:
            if self._paused:
                return None
            conn = self._get_conn()
            with conn:
                cur = conn.execute("SELECT * FROM queued_jobs WHERE state = 'queued' ORDER BY position ASC, created_at ASC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return None
                
                job_id = row["id"]
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE queued_jobs SET state = 'running', started_at = ?, position = 0 WHERE id = ?",
                    (now_str, job_id)
                )
                self._recalculate_positions(conn)
                
                cur = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (job_id,))
                updated_row = cur.fetchone()
                
            refs = self._memory_references.get(job_id, {})
            job = self._row_to_job(
                updated_row,
                msg=refs.get("msg"),
                port=refs.get("port"),
                runner=refs.get("runner") or self._runner
            )
            logger.info("Job %s dequeued (remaining queued count: %d)", job.id, self._get_queued_count(conn))
            return job

    def _get_queued_count(self, conn: sqlite3.Connection) -> int:
        cur = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state = 'queued'")
        return cur.fetchone()[0]

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        """Cancel a queued job by ID."""
        async with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("SELECT * FROM queued_jobs WHERE id = ? AND state = 'queued'", (job_id,))
                row = cur.fetchone()
                if not row:
                    return False, f"未找到队列任务 {job_id}"
                
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE queued_jobs SET state = 'cancelled', finished_at = ?, position = 0 WHERE id = ?",
                    (now_str, job_id)
                )
                self._recalculate_positions(conn)
                
            self._memory_references.pop(job_id, None)
            logger.info("Job %s cancelled from queue", job_id)
            return True, f"已取消队列任务 {job_id}"

    async def clear(self) -> int:
        """Clear all queued jobs. Returns count of cleared jobs."""
        async with self._lock:
            conn = self._get_conn()
            now_str = datetime.now(timezone.utc).isoformat()
            with conn:
                cur = conn.execute("SELECT id FROM queued_jobs WHERE state = 'queued'")
                queued_ids = [r[0] for r in cur.fetchall()]
                if not queued_ids:
                    return 0
                conn.execute(
                    "UPDATE queued_jobs SET state = 'cancelled', finished_at = ?, position = 0 WHERE state = 'queued'",
                    (now_str,)
                )
            for job_id in queued_ids:
                self._memory_references.pop(job_id, None)
            logger.info("Queue cleared (%d jobs removed)", len(queued_ids))
            return len(queued_ids)

    async def pause(self) -> None:
        """Pause automatic dequeue."""
        async with self._lock:
            self._paused = True
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('paused', 'true')"
                )
            logger.info("Queue paused")

    async def resume(self) -> None:
        """Resume automatic dequeue."""
        async with self._lock:
            self._paused = False
            conn = self._get_conn()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO queue_metadata (key, value) VALUES ('paused', 'false')"
                )
            logger.info("Queue resumed")

    async def get_queue_status(self) -> str:
        """Get a formatted queue status string with summaries and queued jobs."""
        async with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("SELECT state, COUNT(*) FROM queued_jobs GROUP BY state")
                counts = {state: 0 for state in ["queued", "running", "interrupted", "completed", "failed", "cancelled"]}
                for row in cur.fetchall():
                    state_val = row[0]
                    if state_val in counts:
                        counts[state_val] = row[1]
                
                cur = conn.execute("SELECT * FROM queued_jobs WHERE state = 'queued' ORDER BY position ASC, created_at ASC")
                queued_rows = cur.fetchall()
                
            lines = [
                f"📋 任务队列状态",
                f"运行状态: {'已暂停' if self._paused else '运行中'}",
                f"排队中 (queued): {counts['queued']}/{self._max_length}",
                f"正在运行 (running): {counts['running']}",
                f"已中断 (interrupted): {counts['interrupted']}",
                f"已完成 (completed): {counts['completed']}",
                f"已失败 (failed): {counts['failed']}",
                f"已取消 (cancelled): {counts['cancelled']}",
            ]
            
            if queued_rows:
                lines.append("")
                lines.append("排队中的任务:")
                for r in queued_rows:
                    job = self._row_to_job(r)
                    lines.append(
                        f"  #{job.position} [{job.id}] {job.mode}\n"
                        f"    提示: {job.prompt_preview}\n"
                        f"    来源: {job.channel}/{job.chat_id[:8]}...\n"
                        f"    创建: {job.created_at.strftime('%H:%M:%S')}"
                    )
            else:
                lines.append("")
                lines.append("📋 任务队列为空")
                
            return "\n".join(lines)

    async def get_job(self, job_id: str) -> QueuedJob | None:
        """Get a queued job by ID."""
        async with self._lock:
            conn = self._get_conn()
            with conn:
                cur = conn.execute("SELECT * FROM queued_jobs WHERE id = ?", (job_id,))
                row = cur.fetchone()
                if not row:
                    return None
            refs = self._memory_references.get(job_id, {})
            return self._row_to_job(
                row,
                msg=refs.get("msg"),
                port=refs.get("port"),
                runner=refs.get("runner") or self._runner
            )

    async def on_job_completed(self, job_id: str | None = None) -> None:
        """Called when a Codex job completes. Starts the next queued job if any."""
        conn = self._get_conn()
        now_str = datetime.now(timezone.utc).isoformat()
        
        error_msg = None
        state = QueueJobState.COMPLETED
        if self._runner and self._runner.current_job:
            current_job = self._runner.current_job
            if job_id is None or str(getattr(current_job, "id", "")) == job_id:
                if current_job.error:
                    error_msg = current_job.error
                    state = QueueJobState.FAILED
                elif current_job.state == JobState.FAILED:
                    state = QueueJobState.FAILED
        
        with conn:
            cur = conn.execute("SELECT id FROM queued_jobs WHERE state = 'running' LIMIT 1")
            row = cur.fetchone()
            if row:
                running_id = row[0]
                conn.execute(
                    "UPDATE queued_jobs SET state = ?, finished_at = ?, error = ? WHERE id = ?",
                    (state.value, now_str, error_msg, running_id)
                )
                self._memory_references.pop(running_id, None)

        if self._paused:
            logger.debug("Queue paused, not starting next job")
            return

        next_job = await self.dequeue()
        if next_job is None:
            return

        if self._start_callback is None:
            logger.warning("No start callback set, cannot start queued job %s", next_job.id)
            return

        logger.info("Starting queued job %s", next_job.id)
        try:
            await self._start_callback(next_job)
        except Exception as exc:
            logger.exception("Failed to start queued job %s", next_job.id)
            with conn:
                conn.execute(
                    "UPDATE queued_jobs SET state = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (now_str, str(exc), next_job.id)
                )


# Global job queue instance
_job_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    """Get the global job queue instance."""
    global _job_queue
    if _job_queue is None:
        _job_queue = JobQueue()
    return _job_queue


def reset_job_queue() -> None:
    """Reset the global job queue (for testing)."""
    global _job_queue
    _job_queue = None
