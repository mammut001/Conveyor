"""mcp_client/config.py — Configuration loading and validation for MCP servers."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp_client.types import ServerConfig

logger = logging.getLogger("conveyor.mcp.config")

SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_SERVERS = 20


def clean_target(server: ServerConfig) -> str:
    """Format safe target description without secrets or query parameters."""
    if server.transport == "stdio":
        if not server.command:
            return "stdio"
        cmd_name = Path(server.command).name
        n_args = len(server.args)
        arg_str = f"{n_args} arg" if n_args == 1 else f"{n_args} args"
        return f"{cmd_name} ({arg_str})"
    elif server.transport == "http":
        if not server.url:
            return "http"
        try:
            parsed = urlsplit(server.url)
            hostname = parsed.hostname or ""
            netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except Exception:
            return server.url
    return ""


def validate_server(name: str, raw: dict, index: int) -> ServerConfig:
    """Validate a single server dictionary. Returns ServerConfig with validation_error if invalid."""
    if index >= MAX_SERVERS:
        return ServerConfig(
            name=name,
            transport=raw.get("transport", "stdio") if isinstance(raw, dict) else "stdio",
            validation_error=f"Maximum {MAX_SERVERS} servers limit exceeded",
        )

    if not isinstance(raw, dict):
        return ServerConfig(
            name=name,
            transport="stdio",
            validation_error="Server definition must be a JSON object",
        )

    if not SERVER_NAME_RE.match(name):
        return ServerConfig(
            name=name,
            transport=str(raw.get("transport", "stdio")),
            validation_error=f"Invalid server name '{name}': must match ^[a-z0-9][a-z0-9_-]{{0,31}}$",
        )

    transport = raw.get("transport")
    if transport not in ("stdio", "http"):
        return ServerConfig(
            name=name,
            transport=str(transport or "stdio"),
            validation_error=f"Invalid transport '{transport}': must be 'stdio' or 'http'",
        )

    command = None
    args = []
    cwd = None
    url = None

    if transport == "stdio":
        raw_cmd = raw.get("command")
        if not isinstance(raw_cmd, str) or not os.path.isabs(raw_cmd):
            return ServerConfig(
                name=name,
                transport=transport,
                validation_error="stdio transport requires an absolute 'command' path",
            )
        command = raw_cmd

        raw_args = raw.get("args", [])
        if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
            return ServerConfig(
                name=name,
                transport=transport,
                command=command,
                validation_error="'args' must be a list of strings",
            )
        args = list(raw_args)

        raw_cwd = raw.get("cwd")
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str):
                return ServerConfig(
                    name=name,
                    transport=transport,
                    command=command,
                    args=args,
                    validation_error="'cwd' must be a string",
                )
            cwd = raw_cwd

    elif transport == "http":
        raw_url = raw.get("url")
        if not isinstance(raw_url, str) or not (
            raw_url.startswith("http://") or raw_url.startswith("https://")
        ):
            return ServerConfig(
                name=name,
                transport=transport,
                validation_error="http transport requires a URL starting with http:// or https://",
            )
        url = raw_url

    # allow_tools validation
    if "allow_tools" not in raw:
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            validation_error="'allow_tools' is required (list of strings or ['*'])",
        )
    raw_allow = raw.get("allow_tools")
    if not isinstance(raw_allow, list) or not all(isinstance(t, str) for t in raw_allow):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            validation_error="'allow_tools' must be a list of strings",
        )
    allow_tools = list(raw_allow)

    # read_only_tools validation
    raw_ro = raw.get("read_only_tools", [])
    if not isinstance(raw_ro, list) or not all(isinstance(t, str) for t in raw_ro):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            validation_error="'read_only_tools' must be a list of strings",
        )
    if "*" in raw_ro:
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            validation_error="'read_only_tools' cannot contain '*' wildcard",
        )
    read_only_tools = list(raw_ro)

    # env and env_from validation
    raw_env = raw.get("env", {})
    if not isinstance(raw_env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_env.items()
    ):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'env' must be a mapping of string to string",
        )
    env = {str(k): str(v) for k, v in raw_env.items()}

    raw_env_from = raw.get("env_from", {})
    if not isinstance(raw_env_from, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_env_from.items()
    ):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'env_from' must be a mapping of string to string",
        )
    for child_k, host_k in raw_env_from.items():
        if not host_k.startswith("MCP_"):
            return ServerConfig(
                name=name,
                transport=transport,
                command=command,
                args=args,
                cwd=cwd,
                url=url,
                allow_tools=allow_tools,
                read_only_tools=read_only_tools,
                validation_error=f"env_from '{child_k}' references invalid env var '{host_k}': must start with MCP_",
            )
    env_from = {str(k): str(v) for k, v in raw_env_from.items()}

    # headers and headers_from validation
    raw_headers = raw.get("headers", {})
    if not isinstance(raw_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_headers.items()
    ):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'headers' must be a mapping of string to string",
        )
    headers = {str(k): str(v) for k, v in raw_headers.items()}

    raw_headers_from = raw.get("headers_from", {})
    if not isinstance(raw_headers_from, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_headers_from.items()
    ):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'headers_from' must be a mapping of string to string",
        )
    for header_k, host_k in raw_headers_from.items():
        if not host_k.startswith("MCP_"):
            return ServerConfig(
                name=name,
                transport=transport,
                command=command,
                args=args,
                cwd=cwd,
                url=url,
                allow_tools=allow_tools,
                read_only_tools=read_only_tools,
                validation_error=f"headers_from '{header_k}' references invalid env var '{host_k}': must start with MCP_",
            )
    headers_from = {str(k): str(v) for k, v in raw_headers_from.items()}

    # timeout_seconds validation
    raw_timeout = raw.get("timeout_seconds", 30)
    if not isinstance(raw_timeout, int) or isinstance(raw_timeout, bool) or raw_timeout < 1 or raw_timeout > 120:
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'timeout_seconds' must be an integer between 1 and 120",
        )
    timeout_seconds = int(raw_timeout)

    # max_output_chars validation
    raw_max_chars = raw.get("max_output_chars", 4000)
    if not isinstance(raw_max_chars, int) or isinstance(raw_max_chars, bool) or raw_max_chars < 200 or raw_max_chars > 20000:
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'max_output_chars' must be an integer between 200 and 20000",
        )
    max_output_chars = int(raw_max_chars)

    # enabled validation
    raw_enabled = raw.get("enabled", True)
    if not isinstance(raw_enabled, bool):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'enabled' must be a boolean",
        )
    enabled = bool(raw_enabled)

    # trust_read_only_hint validation
    raw_trust = raw.get("trust_read_only_hint", False)
    if not isinstance(raw_trust, bool):
        return ServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=args,
            cwd=cwd,
            url=url,
            allow_tools=allow_tools,
            read_only_tools=read_only_tools,
            validation_error="'trust_read_only_hint' must be a boolean",
        )
    trust_read_only_hint = bool(raw_trust)

    return ServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args,
        cwd=cwd,
        url=url,
        env=env,
        env_from=env_from,
        headers=headers,
        headers_from=headers_from,
        allow_tools=allow_tools,
        read_only_tools=read_only_tools,
        trust_read_only_hint=trust_read_only_hint,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        enabled=enabled,
        validation_error=None,
    )


def load_mcp_servers(config_path: Path) -> tuple[dict[str, ServerConfig], str | None]:
    """Load and validate MCP servers from the JSON configuration file.

    Returns (servers_dict, file_error).
    If file is missing: returns ({}, None).
    If file has syntax or schema error: logs error and returns (servers_dict, error_message).
    """
    if not config_path.is_file():
        return {}, None

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as exc:
        err_msg = f"Failed to parse MCP config JSON file: {exc}"
        logger.error(err_msg)
        return {}, err_msg

    if not isinstance(data, dict):
        err_msg = "MCP config root must be a JSON object"
        logger.error(err_msg)
        return {}, err_msg

    raw_servers = data.get("servers", {})
    if not isinstance(raw_servers, dict):
        err_msg = "MCP config 'servers' field must be a JSON object"
        logger.error(err_msg)
        return {}, err_msg

    servers: dict[str, ServerConfig] = {}
    for idx, (name, raw_cfg) in enumerate(raw_servers.items()):
        cfg = validate_server(str(name), raw_cfg, idx)
        servers[cfg.name] = cfg

    return servers, None


def state_file_path(memory_root: Path) -> Path:
    return memory_root / "mcp_state.json"


def load_disabled_servers(memory_root: Path) -> set[str]:
    """Read set of server names explicitly disabled by operator via Web UI."""
    path = state_file_path(memory_root)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("disabled"), list):
            return {str(x) for x in data["disabled"]}
    except Exception:
        logger.warning("Failed to parse %s; treating disabled set as empty", path)
    return set()


def save_disabled_servers(memory_root: Path, disabled: set[str]) -> None:
    """Save disabled server names to 0600 state file atomically."""
    path = state_file_path(memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"disabled": sorted(disabled)}, indent=2).encode("utf-8")
    tmp_path = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_server_effective_enabled(server: ServerConfig, disabled_set: set[str]) -> bool:
    """Server is effectively enabled if enabled in config, not in disabled state, and valid."""
    return server.enabled and (server.name not in disabled_set) and (server.validation_error is None)
