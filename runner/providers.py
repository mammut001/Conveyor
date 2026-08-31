"""Backend selection helpers.

Selection is opt-in. Existing entrypoints keep constructing ``CodexRunner``
unless they explicitly adopt this factory.
"""
from __future__ import annotations

import os
from typing import Any

from runner.backend import CodexBackend
from runner.claude_code import ClaudeCodeBackend
from runner.core import CodexRunner

SUPPORTED_BACKENDS = ("codex", "claude-code")


def create_agent_backend(settings: Any, provider: str | None = None):
    selected = (provider or os.getenv("CONVEYOR_AGENT_BACKEND", "codex")).strip().lower()
    if selected == "codex":
        return CodexBackend(CodexRunner(settings))
    if selected in {"claude", "claude-code"}:
        return ClaudeCodeBackend(settings)
    raise ValueError(f"Unsupported agent backend: {selected}. Expected one of: {', '.join(SUPPORTED_BACKENDS)}")
