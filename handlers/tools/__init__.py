"""handlers/tools — structured tool registry for Conveyor agent layer."""
from handlers.tools import executors as _executors  # noqa: F401 — register tools
from handlers.tools.registry import TOOL_REGISTRY, DangerLevel, ToolSpec, get_tool
from handlers.tools.runner import (
    handle_hybrid,
    handle_route,
    parse_tool_callback,
    try_resolve_confirmation,
    execute_confirmed,
    cancel_pending,
)

__all__ = [
    "TOOL_REGISTRY",
    "DangerLevel",
    "ToolSpec",
    "get_tool",
    "handle_route",
    "handle_hybrid",
    "try_resolve_confirmation",
    "parse_tool_callback",
    "execute_confirmed",
    "cancel_pending",
]
