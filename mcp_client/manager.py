"""mcp_client/manager.py — MCP Manager for caching, schema generation, and tool execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

from config import Settings
from handlers.tools.registry import DangerLevel
from mcp_client.client import execute_call_tool, execute_list_tools, MCPClientError
from mcp_client.config import (
    clean_target,
    is_server_effective_enabled,
    load_disabled_servers,
    load_mcp_servers,
    save_disabled_servers,
)
from mcp_client.types import CachedServerState, MCPToolSpec, ServerConfig
from redaction import redact_text

logger = logging.getLogger("conveyor.mcp.manager")

CACHE_TTL_SECONDS = 300.0
ERROR_RETRY_SECONDS = 60.0


def _resolve_config_path(settings: Settings) -> Path:
    if getattr(settings, "mcp_config_path", None):
        return Path(settings.mcp_config_path).expanduser().resolve()
    mem = getattr(settings, "codex_memory_root", Path("~/.codex"))
    return (Path(mem).expanduser().resolve() / "mcp_servers.json")


def _is_tool_read_only(server: ServerConfig, tool_name: str, annotations: dict[str, Any]) -> bool:
    if tool_name in server.read_only_tools:
        return True
    if server.trust_read_only_hint:
        if annotations.get("readOnlyHint") is True and not annotations.get("destructiveHint"):
            return True
    return False


def _tool_to_func_name(server_name: str, tool_name: str) -> str:
    """Map server name and tool name to a safe OpenAI function name ^[a-zA-Z0-9_-]{1,64}$."""
    # Internal tool format is mcp.<server>.<tool>
    # Function name format: mcp__<server>__<sanitized_tool>
    raw = f"mcp__{server_name}__{tool_name}"
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return sanitized


class MCPManager:
    def __init__(self) -> None:
        self._cache: dict[str, CachedServerState] = {}

    def prime_cache(self, server_name: str, tools: list[MCPToolSpec], status: str = "ok") -> None:
        """Helper for testing or explicitly caching server tools."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self._cache[server_name] = CachedServerState(
            tools=tools,
            checked_at=now_iso,
            status=status,
            error=None,
            cached_at=time.time(),
        )

    def _convert_raw_tool(self, server: ServerConfig, raw_tool: dict[str, Any]) -> MCPToolSpec:
        name = str(raw_tool.get("name", ""))
        desc = str(raw_tool.get("description", ""))
        annotations = raw_tool.get("annotations", {}) if isinstance(raw_tool.get("annotations"), dict) else {}
        input_schema = raw_tool.get("inputSchema", {}) if isinstance(raw_tool.get("inputSchema"), dict) else {}

        read_only = _is_tool_read_only(server, name, annotations)
        danger = DangerLevel.READ if read_only else DangerLevel.WRITE

        clean_desc = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", desc).strip()
        summary = f"[MCP {server.name}] {clean_desc}".strip() if clean_desc else f"[MCP {server.name}] {name}"

        return MCPToolSpec(
            name=f"mcp.{server.name}.{name}",
            server_name=server.name,
            tool_name=name,
            summary=summary,
            danger=danger,
            input_schema=input_schema,
            read_only=read_only,
            annotations=annotations,
            description=clean_desc,
        )

    async def ensure_fresh(self, settings: Settings) -> None:
        """Refresh missing or stale tool lists of enabled servers (concurrently, bounded).

        Called before each chat tool loop so every process (web console, Telegram/Feishu
        bot, routines) discovers tools on its own; failures are cached for a shorter time.
        """
        if not getattr(settings, "mcp_enabled", False):
            return
        servers, _ = load_mcp_servers(_resolve_config_path(settings))
        disabled_set = load_disabled_servers(settings.codex_memory_root)
        now = time.time()
        stale: list[str] = []
        for name, server in servers.items():
            if server.validation_error or not is_server_effective_enabled(server, disabled_set):
                continue
            cached = self._cache.get(name)
            ttl = CACHE_TTL_SECONDS if (cached and cached.status == "ok") else ERROR_RETRY_SECONDS
            if cached is None or now - cached.cached_at > ttl:
                stale.append(name)
        if not stale:
            return
        await asyncio.gather(*(self.refresh_server(settings, name) for name in stale), return_exceptions=True)

    def get_servers_status(self, settings: Settings) -> dict[str, Any]:
        """Produce the summary dictionary for GET /api/mcp/servers."""
        cfg_path = _resolve_config_path(settings)
        servers, cfg_error = load_mcp_servers(cfg_path)
        disabled_set = load_disabled_servers(settings.codex_memory_root)

        items: list[dict[str, Any]] = []
        for name, server in servers.items():
            effective_enabled = is_server_effective_enabled(server, disabled_set)

            cached = self._cache.get(name)
            tools_list: list[MCPToolSpec] = cached.tools if cached else []

            if server.validation_error:
                status = "config_error"
                error = server.validation_error
            elif not server.enabled or name in disabled_set:
                status = "disabled"
                error = None
            elif cached:
                status = cached.status
                error = cached.error
            else:
                status = "unknown"
                error = None

            formatted_tools: list[dict[str, Any]] = []
            for t in tools_list:
                exposed = effective_enabled and (
                    "*" in server.allow_tools or t.tool_name in server.allow_tools
                )
                formatted_tools.append({
                    "name": t.tool_name,
                    "exposed": exposed,
                    "read_only": t.read_only,
                    "description": t.description[:200],
                })

            items.append({
                "name": server.name,
                "transport": server.transport,
                "target": clean_target(server),
                "enabled": effective_enabled,
                "status": status,
                "error": redact_text(error)[:200] if error else None,
                "tool_count": len(formatted_tools),
                "tools": formatted_tools,
                "checked_at": cached.checked_at if cached else None,
            })

        return {
            "items": items,
            "config_path": str(cfg_path),
            "count": len(items),
        }

    async def refresh_server(self, settings: Settings, server_name: str) -> tuple[dict[str, Any], int]:
        """Connect to the named server, list its tools, update cache, and return server item."""
        cfg_path = _resolve_config_path(settings)
        servers, _ = load_mcp_servers(cfg_path)
        disabled_set = load_disabled_servers(settings.codex_memory_root)

        if server_name not in servers:
            return {"error": f"server '{server_name}' not found"}, 404

        server = servers[server_name]
        effective_enabled = is_server_effective_enabled(server, disabled_set)

        if server.validation_error:
            item = {
                "name": server.name,
                "transport": server.transport,
                "target": clean_target(server),
                "enabled": False,
                "status": "config_error",
                "error": server.validation_error,
                "tool_count": 0,
                "tools": [],
                "checked_at": None,
            }
            return item, 400

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            raw_tools = await execute_list_tools(server, settings.codex_memory_root)
            specs = [self._convert_raw_tool(server, t) for t in raw_tools]

            self._cache[server_name] = CachedServerState(
                tools=specs,
                checked_at=now_iso,
                status="ok",
                error=None,
                cached_at=time.time(),
            )

            status = "disabled" if not effective_enabled else "ok"
            formatted_tools = [{
                "name": t.tool_name,
                "exposed": effective_enabled and ("*" in server.allow_tools or t.tool_name in server.allow_tools),
                "read_only": t.read_only,
                "description": t.description[:200],
            } for t in specs]

            item = {
                "name": server.name,
                "transport": server.transport,
                "target": clean_target(server),
                "enabled": effective_enabled,
                "status": status,
                "error": None,
                "tool_count": len(formatted_tools),
                "tools": formatted_tools,
                "checked_at": now_iso,
            }
            return item, 200

        except Exception as exc:
            err_msg = redact_text(str(exc))[:200]
            logger.warning("Failed to refresh MCP server %s: %s", server_name, err_msg)
            self._cache[server_name] = CachedServerState(
                tools=[],
                checked_at=now_iso,
                status="error",
                error=err_msg,
                cached_at=time.time(),
            )
            item = {
                "name": server.name,
                "transport": server.transport,
                "target": clean_target(server),
                "enabled": effective_enabled,
                "status": "error",
                "error": err_msg,
                "tool_count": 0,
                "tools": [],
                "checked_at": now_iso,
            }
            return item, 502

    def set_server_enabled(self, settings: Settings, server_name: str, enabled: bool) -> tuple[dict[str, Any], int]:
        """Update mcp_state.json with server's enabled/disabled state."""
        cfg_path = _resolve_config_path(settings)
        servers, _ = load_mcp_servers(cfg_path)
        if server_name not in servers:
            return {"error": f"server '{server_name}' not found"}, 404

        server = servers[server_name]
        disabled_set = load_disabled_servers(settings.codex_memory_root)

        if enabled:
            disabled_set.discard(server_name)
        else:
            disabled_set.add(server_name)

        save_disabled_servers(settings.codex_memory_root, disabled_set)
        effective_enabled = is_server_effective_enabled(server, disabled_set)

        cached = self._cache.get(server_name)
        tools_list = cached.tools if cached else []

        if server.validation_error:
            status = "config_error"
            error = server.validation_error
        elif not effective_enabled:
            status = "disabled"
            error = None
        elif cached:
            status = cached.status
            error = cached.error
        else:
            status = "unknown"
            error = None

        formatted_tools = [{
            "name": t.tool_name,
            "exposed": effective_enabled and ("*" in server.allow_tools or t.tool_name in server.allow_tools),
            "read_only": t.read_only,
            "description": t.description[:200],
        } for t in tools_list]

        item = {
            "name": server.name,
            "transport": server.transport,
            "target": clean_target(server),
            "enabled": effective_enabled,
            "status": status,
            "error": redact_text(error)[:200] if error else None,
            "tool_count": len(formatted_tools),
            "tools": formatted_tools,
            "checked_at": cached.checked_at if cached else None,
        }
        return item, 200

    def get_tool_spec(self, settings: Settings | None, full_tool_name: str) -> MCPToolSpec | None:
        """Find ToolSpec for an exposed MCP tool."""
        if not settings or not getattr(settings, "mcp_enabled", False):
            return None
        if not full_tool_name.startswith("mcp."):
            return None

        parts = full_tool_name.split(".", 2)
        if len(parts) != 3:
            return None
        server_name, tool_name = parts[1], parts[2]

        cfg_path = _resolve_config_path(settings)
        servers, _ = load_mcp_servers(cfg_path)
        if server_name not in servers:
            return None
        server = servers[server_name]

        disabled_set = load_disabled_servers(settings.codex_memory_root)
        if not is_server_effective_enabled(server, disabled_set):
            return None

        if "*" not in server.allow_tools and tool_name not in server.allow_tools:
            return None

        cached = self._cache.get(server_name)
        if not cached:
            # Fall back to synthesising a basic spec if server is valid & allowlisted
            read_only = tool_name in server.read_only_tools
            danger = DangerLevel.READ if read_only else DangerLevel.WRITE
            return MCPToolSpec(
                name=full_tool_name,
                server_name=server_name,
                tool_name=tool_name,
                summary=f"[MCP {server_name}] {tool_name}",
                danger=danger,
                input_schema={"type": "object", "properties": {}},
                read_only=read_only,
            )

        for t in cached.tools:
            if t.tool_name == tool_name:
                return t

        return None

    def get_exposed_schemas(self, settings: Settings) -> list[dict[str, Any]]:
        """Build OpenAI function schemas for all exposed MCP tools across enabled servers."""
        if not getattr(settings, "mcp_enabled", False):
            return []

        from handlers.chat_tools import REVERSE_TOOL_MAP

        cfg_path = _resolve_config_path(settings)
        servers, _ = load_mcp_servers(cfg_path)
        disabled_set = load_disabled_servers(settings.codex_memory_root)

        schemas: list[dict[str, Any]] = []

        for server_name, server in servers.items():
            if not is_server_effective_enabled(server, disabled_set):
                continue

            cached = self._cache.get(server_name)
            if not cached or cached.status != "ok":
                continue

            for tool in cached.tools:
                # Check allowlist
                if "*" not in server.allow_tools and tool.tool_name not in server.allow_tools:
                    continue

                func_name = _tool_to_func_name(server_name, tool.tool_name)
                if len(func_name) > 64 or not re.match(r"^[a-zA-Z0-9_-]{1,64}$", func_name):
                    logger.warning(
                        "Skipping MCP tool %s: function name '%s' exceeds 64 chars or invalid format",
                        tool.name, func_name,
                    )
                    continue

                if func_name in REVERSE_TOOL_MAP and REVERSE_TOOL_MAP[func_name] != tool.name:
                    logger.warning(
                        "Collision mapping function name '%s' for tool %s (already mapped to %s)",
                        func_name, tool.name, REVERSE_TOOL_MAP[func_name],
                    )
                    continue

                REVERSE_TOOL_MAP[func_name] = tool.name

                schema_raw = tool.input_schema
                if isinstance(schema_raw, dict) and schema_raw.get("type") == "object":
                    try:
                        if len(json.dumps(schema_raw)) <= 8192:
                            parameters = schema_raw
                        else:
                            parameters = {"type": "object", "properties": {}}
                    except Exception:
                        parameters = {"type": "object", "properties": {}}
                else:
                    parameters = {"type": "object", "properties": {}}

                clean_desc = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", tool.description).strip()[:500]
                danger_str = tool.danger.value if hasattr(tool.danger, "value") else str(tool.danger)
                if clean_desc:
                    desc = f"[MCP {server_name}] {clean_desc} [{danger_str}]"
                else:
                    desc = f"[MCP {server_name}] {tool.tool_name} [{danger_str}]"

                schemas.append({
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "description": desc,
                        "parameters": parameters,
                    },
                })

        return schemas

    async def call_tool(self, settings: Settings, full_tool_name: str, arg: str) -> str:
        """Re-validate policies and invoke MCP tool in an ephemeral session."""
        if not getattr(settings, "mcp_enabled", False):
            return f"工具 {full_tool_name} 执行失败: MCP connectors are disabled"

        parts = full_tool_name.split(".", 2)
        if len(parts) != 3:
            return f"未知工具: {full_tool_name}"
        server_name, tool_name = parts[1], parts[2]

        cfg_path = _resolve_config_path(settings)
        servers, _ = load_mcp_servers(cfg_path)
        if server_name not in servers:
            return f"工具 {full_tool_name} 执行失败: server '{server_name}' not found"

        server = servers[server_name]
        disabled_set = load_disabled_servers(settings.codex_memory_root)

        if not is_server_effective_enabled(server, disabled_set):
            return f"工具 {full_tool_name} 执行失败: server '{server_name}' is disabled or has configuration errors"

        if "*" not in server.allow_tools and tool_name not in server.allow_tools:
            return f"工具 {full_tool_name} 执行失败: tool '{tool_name}' is not in server allowlist"

        arguments: dict[str, Any] = {}
        if arg.strip():
            try:
                parsed = json.loads(arg)
                if isinstance(parsed, dict):
                    arguments = parsed
                else:
                    return f"工具 {full_tool_name} 执行失败: arguments must be a JSON object"
            except Exception as exc:
                return f"工具 {full_tool_name} 执行失败: invalid JSON arguments ({exc})"

        try:
            return await execute_call_tool(server, settings.codex_memory_root, tool_name, arguments)
        except MCPClientError as exc:
            logger.warning("MCP call failed for %s: %s", full_tool_name, exc)
            return f"工具 {full_tool_name} 执行失败: {exc}"
        except Exception as exc:
            logger.exception("Unexpected error executing MCP tool %s", full_tool_name)
            return f"工具 {full_tool_name} 执行失败: {type(exc).__name__}"


_GLOBAL_MANAGER: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _GLOBAL_MANAGER
    if _GLOBAL_MANAGER is None:
        _GLOBAL_MANAGER = MCPManager()
    return _GLOBAL_MANAGER
