"""mcp_client/types.py — Type definitions for MCP client and manager."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from handlers.tools.registry import DangerLevel


@dataclass
class ServerConfig:
    name: str
    transport: str  # "stdio" | "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_from: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    headers_from: dict[str, str] = field(default_factory=dict)
    allow_tools: list[str] = field(default_factory=list)
    read_only_tools: list[str] = field(default_factory=list)
    trust_read_only_hint: bool = False
    timeout_seconds: int = 30
    max_output_chars: int = 4000
    enabled: bool = True
    validation_error: str | None = None


@dataclass
class MCPToolSpec:
    name: str  # e.g. "mcp.notes.search"
    server_name: str
    tool_name: str
    summary: str
    danger: DangerLevel
    input_schema: dict[str, Any]
    read_only: bool
    annotations: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class CachedServerState:
    tools: list[MCPToolSpec] = field(default_factory=list)
    checked_at: str | None = None
    status: str = "unknown"  # "ok" | "error" | "config_error" | "disabled" | "unknown"
    error: str | None = None
    cached_at: float = 0.0
