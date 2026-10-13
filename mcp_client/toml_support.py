"""TOML compatibility and strict policy for Conveyor's existing MCP connector.

JSON configurations remain compatible. TOML servers are explicitly allowlisted and
never inherit environment variables not named in their config.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.9/3.10
    import tomli as tomllib

from mcp_client.types import ServerConfig

_LEVELS = frozenset({"READ", "WRITE", "DESTRUCTIVE"})


def parse_toml(text: str) -> dict[str, Any]:
    doc = tomllib.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("servers"), dict):
        raise ValueError("TOML config requires a [servers.<name>] table")
    return doc


def normalize_toml_server(name: str, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize TOML onto the established JSON validator without weakening it."""
    if not isinstance(raw, dict):
        raise ValueError("server must be a TOML table")
    config = dict(raw)
    if config.get("transport") == "streamable_http":
        config["transport"] = "http"
    if "read_only_tools" in config or config.get("trust_read_only_hint"):
        raise ValueError("TOML requires explicit tool_levels; read-only hints are not trusted")
    levels = config.pop("tool_levels", {})
    if not isinstance(levels, dict):
        raise ValueError("tool_levels must be a table")
    for tool, level in levels.items():
        if not isinstance(tool, str) or not tool or not isinstance(level, str) or level.upper() not in _LEVELS:
            raise ValueError("tool_levels entries must map tool names to READ, WRITE or DESTRUCTIVE")
    levels = {key: val.upper() for key, val in levels.items()}
    if "*" in levels:
        raise ValueError("tool_levels cannot use wildcard")
    allow = config.get("allow_tools", [])
    if not isinstance(allow, list) or not all(isinstance(t, str) for t in allow):
        raise ValueError("allow_tools must be a list")
    if any(tool not in allow for tool in levels) and "*" not in allow:
        raise ValueError("tool_levels must refer to allowlisted tools")
    config["allow_tools"] = allow
    config["read_only_tools"] = []  # only tool_levels can grant READ
    config["trust_read_only_hint"] = False
    source = config.pop("env_allowlist", [])
    if not isinstance(source, list) or not all(
        isinstance(name, str) and name.startswith("MCP_") and name.isidentifier() for name in source
    ):
        raise ValueError("env_allowlist must contain only named MCP_* variables")
    mapping = config.get("env_from", {})
    if not isinstance(mapping, dict):
        raise ValueError("env_from must be a table")
    mapping = dict(mapping)
    for var in source:
        if var in mapping and mapping[var] != var:
            raise ValueError("env_allowlist conflicts with env_from")
        mapping[var] = var
    config["env_from"] = mapping
    if config.get("transport") == "stdio" and (config.get("headers") or config.get("headers_from")):
        raise ValueError("stdio must not specify HTTP headers")
    if config.get("transport") == "http" and (config.get("env") or config.get("env_from")):
        raise ValueError("HTTP server must not specify child environment")
    return config, levels


def strict_server(server: ServerConfig, levels: dict[str, str]) -> ServerConfig:
    return replace(server, tool_levels=levels, strict_env=True)
