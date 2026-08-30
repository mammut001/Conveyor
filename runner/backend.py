"""Provider-neutral execution boundary for Conveyor agents.

The existing ``CodexRunner`` remains the implementation and public behavior.
This module introduces a deliberately small protocol so channel/Web code can
stop depending on a concrete provider over time without forcing a runner
rewrite in the same PR.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from runner.types import JobMode

ProgressCallback = Callable[[str], Awaitable[None]]


@runtime_checkable
class AgentBackend(Protocol):
    name: str

    @property
    def current_job(self) -> Any | None: ...

    async def validate(self) -> None: ...

    async def start(self, mode: JobMode, prompt: str, on_progress: ProgressCallback) -> Any: ...

    async def cancel(self) -> str: ...

    async def diff_job(self, job_id: str, worktree: Any | None = None) -> str: ...

    async def apply_job(self, job_id: str, worktree: Any | None = None) -> str: ...

    async def discard_job(self, job_id: str, worktree: Any | None = None) -> str: ...


@dataclass
class CodexBackend:
    """Zero-behavior-change adapter around the existing ``CodexRunner``."""

    runner: Any
    name: str = "codex"

    @property
    def current_job(self) -> Any | None:
        return self.runner.current_job

    @property
    def settings(self) -> Any:
        return self.runner.settings

    async def validate(self) -> None:
        await self.runner.validate()

    async def start(self, mode: JobMode, prompt: str, on_progress: ProgressCallback) -> Any:
        return await self.runner.start(mode, prompt, on_progress)

    async def cancel(self) -> str:
        return await self.runner.cancel()

    async def diff_job(self, job_id: str, worktree: Any | None = None) -> str:
        return await self.runner.diff_job(job_id, worktree)

    async def apply_job(self, job_id: str, worktree: Any | None = None) -> str:
        return await self.runner.apply_job(job_id, worktree)

    async def discard_job(self, job_id: str, worktree: Any | None = None) -> str:
        return await self.runner.discard_job(job_id, worktree)


def backend_name(value: Any) -> str:
    name = str(getattr(value, "name", "") or "").strip().lower()
    if name:
        return name
    if value.__class__.__name__ == "CodexRunner":
        return "codex"
    return value.__class__.__name__.lower()
