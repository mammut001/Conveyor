"""mcp_client — Model Context Protocol connector support for the chat tier."""
from __future__ import annotations

from typing import Any

from config import Settings
from mcp_client.manager import MCPManager, get_mcp_manager
from mcp_client.types import MCPToolSpec, ServerConfig


def is_mcp_tool(tool_name: str) -> bool:
    """Return True if the tool name belongs to an MCP server (mcp.<server>.<tool>)."""
    return tool_name.startswith("mcp.") and len(tool_name.split(".")) == 3


def get_mcp_tool_spec(settings: Settings | None, tool_name: str) -> MCPToolSpec | None:
    """Resolve MCP tool specification if valid and allowed."""
    return get_mcp_manager().get_tool_spec(settings, tool_name)


async def run_mcp_tool(
    settings: Settings,
    tool_name: str,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> str:
    """Execute an MCP tool."""
    return await get_mcp_manager().call_tool(settings, tool_name, arg)


__all__ = [
    "MCPManager",
    "MCPToolSpec",
    "ServerConfig",
    "get_mcp_manager",
    "get_mcp_tool_spec",
    "is_mcp_tool",
    "run_mcp_tool",
]
