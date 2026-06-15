"""personal_tools — Conveyor Personal Tools Hub (P3.1: local notes + reminders).

Future: Gmail, Calendar, Contacts, GitHub integrations register here.
OAuth tokens never enter Codex prompts — only server-side executors.
"""
from personal_tools.registry import (
    PERSONAL_TOOL_REGISTRY,
    execute_personal_tool,
    get_personal_tool,
    register_personal_tools,
    requires_personal_confirmation,
)

register_personal_tools()

__all__ = [
    "PERSONAL_TOOL_REGISTRY",
    "execute_personal_tool",
    "get_personal_tool",
    "register_personal_tools",
    "requires_personal_confirmation",
]
