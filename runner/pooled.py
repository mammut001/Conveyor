from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from runner.backend import AgentBackend, CodexBackend
from runner.claude_code import ClaudeCodeBackend
from runner.cli_backend import CLI_CONFIGS, CliBackend
from runner.core import CodexRunner
from runner.pi_agent import PiBackend
from runner.types import JobMode, ProgressCallback

logger = logging.getLogger("conveyor.runner.pooled")


def create_worker_backend(worker_id: str, settings: Any) -> Any:
    norm = worker_id.strip().lower()
    if norm == "codex":
        return CodexRunner(settings)
    if norm in ("claude", "claude-code"):
        return ClaudeCodeBackend(settings)
    if norm in ("pi", "pi-agent", "pi_agent"):
        return PiBackend(settings)
    if norm in CLI_CONFIGS:
        return CliBackend(settings, CLI_CONFIGS[norm])
    raise ValueError(
        f"Worker '{worker_id}' has no runnable backend "
        "(expected codex, claude-code, pi, agy, grok, or cmd)")


class PooledRunner(CodexRunner):
    """Execution runner that rotates tasks across the configured worker pool."""

    def __init__(self, settings: Any, backend_factory: Any = None) -> None:
        self._current_job: Any = None
        self._last_job: Any = None
        self._active_backend: Any = None
        self._active_worker: dict[str, Any] | None = None
        self._backends: dict[str, Any] = {}
        self._job_backends: dict[str, Any] = {}
        self._job_workers: dict[str, str] = {}
        self._backend_factory = backend_factory or create_worker_backend
        super().__init__(settings)

    def _get_backend(self, worker_id: str) -> Any:
        if worker_id not in self._backends:
            self._backends[worker_id] = self._backend_factory(worker_id, self.settings)
        return self._backends[worker_id]

    @property
    def current_job(self) -> Any | None:
        if self._active_backend is not None and hasattr(self._active_backend, "current_job"):
            return self._active_backend.current_job
        return self._current_job

    @current_job.setter
    def current_job(self, value: Any) -> None:
        self._current_job = value
        if self._active_backend is not None and hasattr(self._active_backend, "current_job"):
            self._active_backend.current_job = value

    @property
    def last_job(self) -> Any | None:
        if self._active_backend is not None and hasattr(self._active_backend, "last_job"):
            return self._active_backend.last_job
        return self._last_job

    @last_job.setter
    def last_job(self, value: Any) -> None:
        self._last_job = value
        if self._active_backend is not None and hasattr(self._active_backend, "last_job"):
            self._active_backend.last_job = value

    async def validate(self) -> None:
        from conveyor.agents.workers import async_pick_worker
        worker = await async_pick_worker(self.settings)
        if not worker:
            raise RuntimeError("No eligible worker available in worker pool")
        backend = self._get_backend(worker["id"])
        if hasattr(backend, "validate"):
            await backend.validate()

    async def start(
        self,
        mode: JobMode,
        prompt: str,
        on_progress: ProgressCallback,
        **kwargs: Any,
    ) -> Any:
        from conveyor.agents.workers import async_pick_worker, classify_failure, mark_cooling

        worker = await async_pick_worker(self.settings, mode=mode)
        if not worker:
            raise RuntimeError("No eligible worker available in worker pool")

        backend = self._get_backend(worker["id"])
        self._active_backend = backend
        self._active_worker = worker

        try:
            job = await backend.start(mode, prompt, on_progress, **kwargs)
            job_id = getattr(job, "id", str(job))
            self._job_backends[job_id] = backend
            self._job_workers[job_id] = worker["id"]
            setattr(job, "_worker_id", worker["id"])
            setattr(job, "_worker_label", worker.get("label", worker["id"]))
            self.last_job = job
            return job
        except Exception as exc:
            failure = classify_failure(str(exc))
            if not failure or failure[0] != "quota":
                raise

            logger.warning("Worker %s encountered quota error at start(): %s", worker["id"], exc)
            mark_cooling(self.settings, worker["id"], reset_seconds=failure[1], reason=str(exc))

            # Retry once on next eligible worker (start-time quota retry)
            next_worker = await async_pick_worker(self.settings, mode=mode)
            if not next_worker or next_worker["id"] == worker["id"]:
                raise

            logger.info("Retrying job once on next eligible worker at start(): %s", next_worker["id"])
            next_backend = self._get_backend(next_worker["id"])
            self._active_backend = next_backend
            self._active_worker = next_worker

            try:
                job = await next_backend.start(mode, prompt, on_progress, **kwargs)
                job_id = getattr(job, "id", str(job))
                self._job_backends[job_id] = next_backend
                self._job_workers[job_id] = next_worker["id"]
                setattr(job, "_worker_id", next_worker["id"])
                setattr(job, "_worker_label", next_worker.get("label", next_worker["id"]))
                self.last_job = job
                return job
            except Exception as retry_exc:
                retry_failure = classify_failure(str(retry_exc))
                if retry_failure and retry_failure[0] == "quota":
                    mark_cooling(
                        self.settings,
                        next_worker["id"],
                        reset_seconds=retry_failure[1],
                        reason=str(retry_exc),
                    )
                raise

    async def handle_job_failure(self, job: Any) -> tuple[str, int] | None:
        from conveyor.agents.workers import classify_failure, mark_cooling

        error_text = f"{getattr(job, 'error', '') or ''} {getattr(job, 'last_event', '') or ''}".strip()
        failure = classify_failure(error_text)
        if failure and failure[0] == "quota":
            job_id = getattr(job, "id", str(job))
            worker_id = self._job_workers.get(job_id) or getattr(job, "_worker_id", None)
            if not worker_id and getattr(self, "_active_worker", None):
                worker_id = self._active_worker.get("id")
            if not worker_id and self._active_backend:
                worker_id = getattr(self._active_backend, "name", None)
            if worker_id:
                mark_cooling(self.settings, worker_id, reset_seconds=failure[1], reason=error_text)
            return failure
        return None

    async def peek_next_worker(self) -> dict[str, Any] | None:
        from conveyor.agents.workers import async_pick_worker
        return await async_pick_worker(self.settings)

    async def has_job_changes(self, job: Any) -> bool:
        worktree = getattr(job, "worktree_path", None)
        if not worktree:
            return False
        worktree_path = Path(worktree)
        if not worktree_path.exists():
            return False
        try:
            diff = await self.diff_job(getattr(job, "id", ""), worktree_path)
            if not diff or not diff.strip():
                return False
            if "no git diff" in diff or "No job worktree available" in diff:
                return False
            if "diff --git" in diff or "(no tracked diff" not in diff:
                return True
            if "(clean)" not in diff:
                return True
            return False
        except Exception:
            return False

    async def cancel(self) -> str:
        if self._active_backend is not None and hasattr(self._active_backend, "cancel"):
            return await self._active_backend.cancel()
        return await super().cancel()

    async def diff_job(self, job_id: str, worktree: Any | None = None) -> str:
        backend = self._job_backends.get(job_id) or self._active_backend
        if backend is not None and hasattr(backend, "diff_job"):
            return await backend.diff_job(job_id, worktree)
        return await super().diff_job(job_id, worktree)

    async def apply_job(self, job_id: str, worktree: Any | None = None) -> str:
        backend = self._job_backends.get(job_id) or self._active_backend
        if backend is not None and hasattr(backend, "apply_job"):
            return await backend.apply_job(job_id, worktree)
        return await super().apply_job(job_id, worktree)

    async def discard_job(self, job_id: str, worktree: Any | None = None) -> str:
        backend = self._job_backends.get(job_id) or self._active_backend
        if backend is not None and hasattr(backend, "discard_job"):
            return await backend.discard_job(job_id, worktree)
        return await super().discard_job(job_id, worktree)
