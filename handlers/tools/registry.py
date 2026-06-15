"""handlers/tools/registry.py — structured tool registry.

Each ToolSpec describes a deterministic capability the bot can invoke
without Codex. Dangerous tools require explicit operator confirmation
before execution (see handlers/tools/confirm.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from config import Settings

ToolExecutor = Callable[[Settings, str], Awaitable[str]]


class DangerLevel(str, Enum):
    READ = "read"           # safe: host snapshots, logs tail, git status
    WRITE_SAFE = "write_safe"  # mutates state but low-risk/soft: audit, no confirmation
    WRITE = "write"         # mutates state: restart service, apply changes
    DESTRUCTIVE = "destructive"  # irreversible: clean, discard


@dataclass(frozen=True)
class ToolSpec:
    name: str
    summary: str
    danger: DangerLevel
    executor: ToolExecutor
    # Natural-language keywords used by the intent router (conservative).
    keywords: tuple[str, ...] = ()


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    TOOL_REGISTRY[spec.name] = spec
    return spec


def get_tool(name: str) -> ToolSpec | None:
    return TOOL_REGISTRY.get(name)


def safe_tool_names() -> tuple[str, ...]:
    return tuple(
        name for name, spec in TOOL_REGISTRY.items()
        if spec.danger == DangerLevel.READ
    )


def requires_confirmation(spec: ToolSpec) -> bool:
    return spec.danger in (DangerLevel.WRITE, DangerLevel.DESTRUCTIVE)


def requires_audit(spec: ToolSpec) -> bool:
    """WRITE_SAFE + WRITE + DESTRUCTIVE are audit-logged."""
    return spec.danger in (DangerLevel.WRITE_SAFE, DangerLevel.WRITE, DangerLevel.DESTRUCTIVE)
