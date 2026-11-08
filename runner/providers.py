"""Backend selection helpers.

Selection is opt-in. Existing entrypoints keep constructing ``CodexRunner``
unless they explicitly adopt this factory or set ``CONVEYOR_AGENT_BACKEND``.
"""
from __future__ import annotations

import os
from typing import Any

from runner.backend import CodexBackend
from runner.claude_code import ClaudeCodeBackend
from runner.core import CodexRunner
from runner.pi_agent import PiBackend

SUPPORTED_BACKENDS = ("codex", "claude-code", "pi")


def create_agent_backend(settings: Any, provider: str | None = None):
    selected = (provider or os.getenv("CONVEYOR_AGENT_BACKEND", "codex")).strip().lower()
    if selected == "codex":
        return CodexBackend(CodexRunner(settings))
    if selected in {"claude", "claude-code"}:
        return ClaudeCodeBackend(settings)
    if selected in {"pi", "pi-agent", "pi_agent"}:
        return PiBackend(settings)
    raise ValueError(f"Unsupported agent backend: {selected}. Expected one of: {', '.join(SUPPORTED_BACKENDS)}")


def create_runtime_runner(settings: Any, provider: str | None = None) -> CodexRunner:
    backend_env = os.getenv("CONVEYOR_AGENT_BACKEND", "").strip()
    workers_env = os.getenv("CONVEYOR_WORKERS_ENABLED", "").strip().lower()
    workers_explicit_true = workers_env in ("true", "1", "yes", "on")

    use_pool = getattr(settings, "workers_enabled", False) and not provider
    if backend_env and not workers_explicit_true:
        use_pool = False

    if use_pool:
        from runner.pooled import PooledRunner
        return PooledRunner(settings)
    selected = (provider or backend_env or "codex").strip().lower()
    if selected in {"", "codex"}:
        return CodexRunner(settings)
    if selected in {"claude", "claude-code"}:
        return ClaudeCodeBackend(settings)
    if selected in {"pi", "pi-agent", "pi_agent"}:
        return PiBackend(settings)
    raise ValueError(f"Unsupported agent backend: {selected}. Expected one of: {', '.join(SUPPORTED_BACKENDS)}")
