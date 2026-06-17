"""handlers/job_queue.py — single-concurrency FIFO queue for Codex jobs.

P3.8: Adds a queue above the runner so new jobs are queued instead of
rejected when a Codex job is running. Actual Codex execution remains
single-concurrency.

Key behaviors:
- In-memory FIFO queue (lost on bot restart, documented).
- Max queue length default 10.
- When a job completes, automatically starts the next queued job.
- Queue only stores prompt text and routing metadata (no secrets).
- Redact/truncate queue display.
- Queue operations are audited when mutating.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from redaction import redact_text, truncate

if TYPE_CHECKING:
    from channel.types import InboundMessage, OutboundPort
    from runner import CodexRunner, JobMode

logger = logging.getLogger(__name__)

# Default max queue length
DEFAULT_MAX_QUEUE_LENGTH = 10

# Queue job states
class QueueJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


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
    """Single-concurrency FIFO queue for Codex jobs."""

    def __init__(self, max_length: int = DEFAULT_MAX_QUEUE_LENGTH) -> None:
        self._queue: list[QueuedJob] = []
        self._max_length = max_length
        self._lock = asyncio.Lock()
        self._paused: bool = False
        self._counter: int = 0
        # Callback to start a job (set by the integration layer)
        self._start_callback: Callable[[QueuedJob], Awaitable[None]] | None = None

    @property
    def is_paused(self) -> bool:
        """Whether automatic dequeue is paused."""
        return self._paused

    @property
    def queue_length(self) -> int:
        """Number of jobs in the queue."""
        return len(self._queue)

    def set_start_callback(self, callback: Callable[[QueuedJob], Awaitable[None]]) -> None:
        """Set the callback to start a queued job."""
        self._start_callback = callback

    def _next_id(self) -> str:
        """Generate a unique queue job ID."""
        self._counter += 1
        return f"q{self._counter}"

    async def enqueue(
        self,
        mode: str,
        prompt: str,
        msg: "InboundMessage",
        port: "OutboundPort",
        runner: "CodexRunner",
        original_text: str | None = None,
    ) -> tuple[bool, str, QueuedJob | None]:
        """Add a job to the queue.

        Returns:
            (success, message, queued_job)
        """
        async with self._lock:
            if len(self._queue) >= self._max_length:
                return False, f"队列已满（最多 {self._max_length} 个任务）", None

            job_id = self._next_id()
            queued_job = QueuedJob(
                id=job_id,
                mode=mode,
                prompt=prompt,
                channel=msg.channel,
                chat_id=msg.chat_id,
                operator_id=msg.operator_id,
                original_text=original_text or msg.text,
                position=len(self._queue) + 1,
                _msg=msg,
                _port=port,
                _runner=runner,
            )
            self._queue.append(queued_job)

            # Update positions for all queued jobs
            for i, job in enumerate(self._queue):
                job.position = i + 1

            logger.info(
                "Job %s queued at position %d (channel=%s, operator=%s)",
                job_id, queued_job.position, msg.channel, msg.operator_id,
            )

            return True, (
                f"⏳ 任务已排队\n"
                f"队列位置: {queued_job.position}/{len(self._queue)}\n"
                f"队列 ID: {job_id}\n"
                f"提示: {queued_job.prompt_preview}"
            ), queued_job

    async def dequeue(self) -> QueuedJob | None:
        """Remove and return the next job from the queue.

        Returns None if queue is empty or paused.
        """
        async with self._lock:
            if not self._queue or self._paused:
                return None

            job = self._queue.pop(0)
            job.state = QueueJobState.RUNNING

            # Update positions for remaining jobs
            for i, remaining in enumerate(self._queue):
                remaining.position = i + 1

            logger.info("Job %s dequeued (remaining: %d)", job.id, len(self._queue))
            return job

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        """Cancel a queued job by ID.

        Returns:
            (success, message)
        """
        async with self._lock:
            for i, job in enumerate(self._queue):
                if job.id == job_id:
                    job.state = QueueJobState.CANCELLED
                    self._queue.pop(i)

                    # Update positions for remaining jobs
                    for j, remaining in enumerate(self._queue):
                        remaining.position = j + 1

                    logger.info("Job %s cancelled from queue", job_id)
                    return True, f"已取消队列任务 {job_id}"

            return False, f"未找到队列任务 {job_id}"

    async def clear(self) -> int:
        """Clear all queued jobs. Returns count of cleared jobs."""
        async with self._lock:
            count = len(self._queue)
            for job in self._queue:
                job.state = QueueJobState.CANCELLED
            self._queue.clear()
            logger.info("Queue cleared (%d jobs removed)", count)
            return count

    async def pause(self) -> None:
        """Pause automatic dequeue."""
        async with self._lock:
            self._paused = True
            logger.info("Queue paused")

    async def resume(self) -> None:
        """Resume automatic dequeue."""
        async with self._lock:
            self._paused = False
            logger.info("Queue resumed")

    async def get_queue_status(self) -> str:
        """Get a formatted queue status string."""
        async with self._lock:
            if not self._queue:
                return "📋 任务队列为空"

            lines = [
                f"📋 任务队列 ({len(self._queue)}/{self._max_length})",
                f"状态: {'已暂停' if self._paused else '运行中'}",
                "",
            ]

            for job in self._queue:
                lines.append(
                    f"  #{job.position} [{job.id}] {job.mode}\n"
                    f"    提示: {job.prompt_preview}\n"
                    f"    来源: {job.channel}/{job.chat_id[:8]}...\n"
                    f"    创建: {job.created_at.strftime('%H:%M:%S')}"
                )

            return "\n".join(lines)

    async def get_job(self, job_id: str) -> QueuedJob | None:
        """Get a queued job by ID."""
        async with self._lock:
            for job in self._queue:
                if job.id == job_id:
                    return job
            return None

    async def on_job_completed(self, job_id: str | None = None) -> None:
        """Called when a Codex job completes. Starts the next queued job if any."""
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
            next_job.state = QueueJobState.FAILED


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
