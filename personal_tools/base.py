"""personal_tools/base.py — foundation types for Conveyor Personal Tools Hub.

Future phases (Gmail, Calendar, Contacts, GitHub) will implement
BasePersonalTool subclasses here. OAuth tokens stay outside Codex
prompt reach — only thin tool executors run server-side.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from config import Settings
from handlers.tools.registry import DangerLevel

__all__ = [
    "DangerLevel",
    "ToolResult",
    "PersonalToolSpec",
    "BasePersonalTool",
]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str


@dataclass(frozen=True)
class PersonalToolSpec:
    """Registry entry for a namespaced personal tool (e.g. notes.add)."""

    name: str
    summary: str
    danger: DangerLevel
    keywords: tuple[str, ...] = ()


class BasePersonalTool(Protocol):
    """Server-side personal tool. Never receives OAuth secrets via Codex."""

    spec: PersonalToolSpec

    async def run(self, settings: Settings, arg: str, *, operator_id: str) -> ToolResult: ...
