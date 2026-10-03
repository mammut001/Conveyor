"""mcp_client/client.py — Minimal JSON-RPC 2.0 client for MCP servers over stdio and HTTP."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp_client.types import MCPToolSpec, ServerConfig
from redaction import redact_text

logger = logging.getLogger("conveyor.mcp.client")

MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 1024 * 1024        # 1 MB per message line
MAX_TOTAL_READ_BYTES = 4 * 1024 * 1024  # 4 MB per session total
STDERR_DRAIN_LIMIT = 4096           # last 4 KB kept for errors
MAX_LIST_PAGES = 10
MAX_TOOLS_COUNT = 200


class MCPClientError(RuntimeError):
    """Base error for MCP client operations."""


class MCPTimeoutError(MCPClientError):
    """Operation timed out."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def format_tool_call_result(result: dict[str, Any], max_chars: int) -> str:
    """Format, redact, and cap an MCP tools/call result."""
    content_items = result.get("content") or []
    text_parts: list[str] = []

    if isinstance(content_items, list):
        for item in content_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(str(item.get("text", "")))
            elif item_type == "image":
                mime = item.get("mimeType", "image")
                text_parts.append(f"[image omitted: {mime}]")
            elif item_type == "audio":
                mime = item.get("mimeType", "audio")
                text_parts.append(f"[audio omitted: {mime}]")
            elif item_type == "resource":
                res = item.get("resource", {})
                if isinstance(res, dict):
                    uri = res.get("uri", "")
                    res_text = res.get("text")
                    if res_text is not None:
                        if uri:
                            text_parts.append(f"[resource: {uri}]\n{res_text}")
                        else:
                            text_parts.append(str(res_text))
                    else:
                        text_parts.append(f"[resource: {uri}]" if uri else "[resource]")
                else:
                    text_parts.append("[resource]")
            else:
                text_parts.append(f"[{item_type or 'content'}]")

    if not text_parts and "structuredContent" in result:
        text_parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))

    combined = "\n".join(text_parts)
    if result.get("isError") is True:
        combined = f"MCP tool error: {combined}"

    redacted = redact_text(combined)

    if len(redacted) > max_chars:
        excess = len(redacted) - max_chars
        redacted = redacted[:max_chars] + f"\n[truncated {excess} chars]"

    return redacted


def build_child_env(server: ServerConfig, memory_root: Path) -> tuple[dict[str, str], Path]:
    """Construct minimal isolated environment for an MCP child process."""
    home_dir = memory_root / "mcp_home" / server.name
    home_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home_dir, 0o700)
    except OSError:
        pass

    env = {
        "HOME": str(home_dir),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    if "LANG" in os.environ:
        env["LANG"] = os.environ["LANG"]
    if "LC_ALL" in os.environ:
        env["LC_ALL"] = os.environ["LC_ALL"]

    for k, v in server.env.items():
        env[str(k)] = str(v)

    for child_k, host_k in server.env_from.items():
        if host_k in os.environ:
            env[str(child_k)] = os.environ[host_k]

    return env, home_dir


# -----------------------------------------------------------------------------
# stdio Client Session
# -----------------------------------------------------------------------------

class StdioSession:
    def __init__(self, server: ServerConfig, memory_root: Path) -> None:
        self.server = server
        self.memory_root = memory_root
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_chunks: list[bytes] = []
        self.stderr_task: asyncio.Task | None = None
        self.total_bytes_read = 0

    async def __aenter__(self) -> StdioSession:
        if not self.server.command:
            raise MCPClientError("stdio server missing command")

        env, home_dir = build_child_env(self.server, self.memory_root)
        cwd = self.server.cwd or str(home_dir)
        if not Path(cwd).exists():
            cwd = str(home_dir)

        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.server.command,
                *self.server.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                # StreamReader's default 64 KiB line limit would reject large (but allowed) messages.
                limit=MAX_LINE_BYTES + 1,
                # Own process group so wrappers like `npx`/`uvx` and their children die with it.
                start_new_session=True,
            )
        except Exception as exc:
            raise MCPClientError(f"Failed to spawn MCP server {self.server.name}: {exc}") from exc

        self.stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def _drain_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        try:
            while True:
                chunk = await self.proc.stderr.read(1024)
                if not chunk:
                    break
                self.stderr_chunks.append(chunk)
                total = sum(len(c) for c in self.stderr_chunks)
                while total > STDERR_DRAIN_LIMIT and len(self.stderr_chunks) > 1:
                    total -= len(self.stderr_chunks.pop(0))
        except Exception:
            pass

    def get_stderr_snippet(self) -> str:
        raw = b"".join(self.stderr_chunks)[-STDERR_DRAIN_LIMIT:]
        text = raw.decode("utf-8", errors="replace").strip()
        return redact_text(text)

    async def send_msg(self, msg: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPClientError("Server process stdin not available")
        payload = json.dumps(msg).encode("utf-8") + b"\n"
        self.proc.stdin.write(payload)
        await self.proc.stdin.drain()

    async def read_response(self, req_id: int | str) -> dict[str, Any]:
        if not self.proc or not self.proc.stdout:
            raise MCPClientError("Server process stdout not available")

        while True:
            try:
                line_bytes = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise MCPClientError("Message line exceeded maximum of 1 MB") from exc
            if not line_bytes:
                err_tail = self.get_stderr_snippet()
                extra = f" (stderr: {err_tail})" if err_tail else ""
                raise MCPClientError(f"MCP server closed stdout unexpectedly{extra}")

            if len(line_bytes) > MAX_LINE_BYTES:
                raise MCPClientError("Message line exceeded maximum of 1 MB")

            self.total_bytes_read += len(line_bytes)
            if self.total_bytes_read > MAX_TOTAL_READ_BYTES:
                raise MCPClientError("Session read limit exceeded 4 MB")

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except Exception:
                # Ignore non-JSON lines
                continue

            if not isinstance(msg, dict):
                continue

            # Server-to-client request: ping or other method
            if "method" in msg and "id" in msg:
                incoming_id = msg["id"]
                if msg.get("method") == "ping":
                    await self.send_msg({"jsonrpc": "2.0", "id": incoming_id, "result": {}})
                else:
                    await self.send_msg({
                        "jsonrpc": "2.0",
                        "id": incoming_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    })
                continue

            # Server-to-client notification: ignore
            if "method" in msg and "id" not in msg:
                continue

            # Response matching our request id
            if msg.get("id") == req_id:
                if "error" in msg:
                    err_info = msg["error"]
                    err_msg = err_info.get("message") if isinstance(err_info, dict) else str(err_info)
                    raise MCPClientError(f"MCP error: {err_msg}")
                return msg.get("result", {})

    async def initialize(self) -> dict[str, Any]:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conveyor", "version": "0.1.0"},
            },
        }
        await self.send_msg(init_req)
        result = await self.read_response(1)
        # Send notifications/initialized
        await self.send_msg({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if self.proc and self.proc.returncode is None:
            try:
                if self.proc.stdin and not self.proc.stdin.is_closing():
                    self.proc.stdin.close()
            except Exception:
                pass
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._signal_group(signal.SIGKILL)
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

    def _signal_group(self, sig: int) -> None:
        if not self.proc:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            try:
                self.proc.send_signal(sig)
            except ProcessLookupError:
                pass


# -----------------------------------------------------------------------------
# HTTP Client Session (Urllib + Thread)
# -----------------------------------------------------------------------------

class HttpSession:
    def __init__(self, server: ServerConfig) -> None:
        self.server = server
        self.session_id: str | None = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.opener = urllib.request.build_opener(NoRedirectHandler)

    def _build_headers(self, is_notification: bool = False) -> dict[str, str]:
        hdrs: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.protocol_version:
            hdrs["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id

        for k, v in self.server.headers.items():
            hdrs[str(k)] = str(v)

        for child_k, host_k in self.server.headers_from.items():
            if host_k in os.environ:
                hdrs[str(child_k)] = os.environ[host_k]

        return hdrs

    def _post_sync(self, msg: dict[str, Any], is_notification: bool = False) -> dict[str, Any] | None:
        if not self.server.url:
            raise MCPClientError("http server missing url")

        headers = self._build_headers(is_notification=is_notification)
        data_bytes = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(
            self.server.url,
            data=data_bytes,
            headers=headers,
            method="POST",
        )

        timeout = min(15.0, float(self.server.timeout_seconds)) if msg.get("method") == "initialize" else float(self.server.timeout_seconds)

        try:
            with self.opener.open(req, timeout=timeout) as resp:
                sess_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sess_id:
                    self.session_id = sess_id

                if is_notification:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                raw_body = resp.read(MAX_TOTAL_READ_BYTES + 1)
                if len(raw_body) > MAX_TOTAL_READ_BYTES:
                    raise MCPClientError("Response exceeded 4 MB limit")

                body_text = raw_body.decode("utf-8", errors="replace")

                if "text/event-stream" in content_type:
                    for line in body_text.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            chunk = line[5:].strip()
                            try:
                                parsed = json.loads(chunk)
                                if isinstance(parsed, dict) and parsed.get("id") == msg.get("id"):
                                    if "error" in parsed:
                                        err = parsed["error"]
                                        err_msg = err.get("message") if isinstance(err, dict) else str(err)
                                        raise MCPClientError(f"MCP error: {err_msg}")
                                    return parsed.get("result", {})
                            except json.JSONDecodeError:
                                continue
                    raise MCPClientError(f"No matching SSE response found for request id {msg.get('id')}")

                try:
                    parsed = json.loads(body_text)
                except Exception as exc:
                    raise MCPClientError(f"Invalid JSON response from HTTP server: {exc}") from exc

                if isinstance(parsed, dict) and parsed.get("id") == msg.get("id"):
                    if "error" in parsed:
                        err = parsed["error"]
                        err_msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise MCPClientError(f"MCP error: {err_msg}")
                    return parsed.get("result", {})

                raise MCPClientError(f"Unexpected response structure: {body_text[:100]}")

        except MCPClientError:
            raise
        except urllib.error.HTTPError as exc:
            # Notifications can return 202/204
            if is_notification and exc.code in (200, 202, 204):
                return None
            raise MCPClientError(f"HTTP error {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise MCPClientError(f"HTTP connection failed: {type(exc).__name__}: {redact_text(str(exc))[:200]}") from exc

    def _delete_sync(self) -> None:
        if not self.server.url or not self.session_id:
            return
        headers = self._build_headers()
        req = urllib.request.Request(
            self.server.url,
            headers=headers,
            method="DELETE",
        )
        try:
            with self.opener.open(req, timeout=5.0):
                pass
        except Exception:
            pass

    async def initialize(self) -> dict[str, Any]:
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "conveyor", "version": "0.1.0"},
            },
        }
        res = await asyncio.to_thread(self._post_sync, init_req, False)
        # Send notifications/initialized
        await asyncio.to_thread(
            self._post_sync,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            True,
        )
        return res or {}

    async def send_request(self, req: dict[str, Any]) -> dict[str, Any]:
        res = await asyncio.to_thread(self._post_sync, req, False)
        return res or {}

    async def close(self) -> None:
        if self.session_id:
            await asyncio.to_thread(self._delete_sync)


# -----------------------------------------------------------------------------
# Public High-Level Client Operations
# -----------------------------------------------------------------------------

async def execute_list_tools(server: ServerConfig, memory_root: Path) -> list[dict[str, Any]]:
    """Connect to server, list all tools with pagination (ephemeral session)."""
    async def _run() -> list[dict[str, Any]]:
        all_tools: list[dict[str, Any]] = []

        if server.transport == "stdio":
            async with StdioSession(server, memory_root) as session:
                init_timeout = min(15.0, float(server.timeout_seconds))
                await asyncio.wait_for(session.initialize(), timeout=init_timeout)

                req_id = 2
                cursor: str | None = None
                pages = 0

                while pages < MAX_LIST_PAGES and len(all_tools) < MAX_TOOLS_COUNT:
                    params: dict[str, Any] = {}
                    if cursor:
                        params["cursor"] = cursor
                    req = {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params}
                    await session.send_msg(req)
                    result = await session.read_response(req_id)
                    req_id += 1
                    pages += 1

                    raw_tools = result.get("tools") or []
                    if isinstance(raw_tools, list):
                        for t in raw_tools:
                            if isinstance(t, dict):
                                all_tools.append(t)
                                if len(all_tools) >= MAX_TOOLS_COUNT:
                                    break

                    cursor = result.get("nextCursor")
                    if not cursor:
                        break

        elif server.transport == "http":
            session = HttpSession(server)
            try:
                init_timeout = min(15.0, float(server.timeout_seconds))
                await asyncio.wait_for(session.initialize(), timeout=init_timeout)

                req_id = 2
                cursor = None
                pages = 0

                while pages < MAX_LIST_PAGES and len(all_tools) < MAX_TOOLS_COUNT:
                    params = {}
                    if cursor:
                        params["cursor"] = cursor
                    req = {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params}
                    result = await session.send_request(req)
                    req_id += 1
                    pages += 1

                    raw_tools = result.get("tools") or []
                    if isinstance(raw_tools, list):
                        for t in raw_tools:
                            if isinstance(t, dict):
                                all_tools.append(t)
                                if len(all_tools) >= MAX_TOOLS_COUNT:
                                    break

                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
            finally:
                await session.close()
        else:
            raise MCPClientError(f"Unsupported transport: {server.transport}")

        return all_tools[:MAX_TOOLS_COUNT]

    try:
        return await asyncio.wait_for(_run(), timeout=float(server.timeout_seconds))
    except asyncio.TimeoutError as exc:
        raise MCPTimeoutError(f"Listing tools timed out after {server.timeout_seconds}s") from exc


async def execute_call_tool(
    server: ServerConfig,
    memory_root: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Connect to server, execute tools/call, format, redact, and cap output."""
    async def _run() -> str:
        if server.transport == "stdio":
            async with StdioSession(server, memory_root) as session:
                init_timeout = min(15.0, float(server.timeout_seconds))
                await asyncio.wait_for(session.initialize(), timeout=init_timeout)

                req = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
                await session.send_msg(req)
                result = await session.read_response(2)
                return format_tool_call_result(result, server.max_output_chars)

        elif server.transport == "http":
            session = HttpSession(server)
            try:
                init_timeout = min(15.0, float(server.timeout_seconds))
                await asyncio.wait_for(session.initialize(), timeout=init_timeout)

                req = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                }
                result = await session.send_request(req)
                return format_tool_call_result(result, server.max_output_chars)
            finally:
                await session.close()
        else:
            raise MCPClientError(f"Unsupported transport: {server.transport}")

    try:
        return await asyncio.wait_for(_run(), timeout=float(server.timeout_seconds))
    except asyncio.TimeoutError as exc:
        raise MCPTimeoutError(f"Tool execution timed out after {server.timeout_seconds}s") from exc
