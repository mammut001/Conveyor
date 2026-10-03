#!/usr/bin/env python3
"""tests/fixtures/mcp_test_server.py — Test MCP server fixture implementing JSON-RPC 2.0.

Supports:
- --stdio
- --http --port <N> [--sse] [--paginate] [--notes <path>]
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import time
import uuid
from typing import Any

ALL_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back input text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add",
        "description": "Add two numbers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "Write a note to persistent storage",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        # No annotations -> WRITE danger by default
    },
    {
        "name": "env_probe",
        "description": "Probe environment variables present in process",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vars": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "big",
        "description": "Return large output",
        "inputSchema": {
            "type": "object",
            "properties": {"chars": {"type": "integer"}},
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "slow",
        "description": "Sleep for requested seconds",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "fail",
        "description": "Tool that returns error",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]


def handle_tool_call(name: str, args: dict[str, Any], notes_file: str | None) -> dict[str, Any]:
    if name == "echo":
        return {
            "content": [{"type": "text", "text": str(args.get("text", ""))}],
            "isError": False,
        }
    elif name == "add":
        a = float(args.get("a", 0))
        b = float(args.get("b", 0))
        res = a + b
        if res.is_integer():
            res_str = str(int(res))
        else:
            res_str = str(res)
        return {
            "content": [{"type": "text", "text": res_str}],
            "isError": False,
        }
    elif name == "write_note":
        text = str(args.get("text", ""))
        filepath = notes_file or os.environ.get("MCP_TEST_NOTES_FILE", "/tmp/mcp_test_notes.txt")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(text + "\n")
            return {
                "content": [{"type": "text", "text": "saved"}],
                "isError": False,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"write error: {exc}"}],
                "isError": True,
            }
    elif name == "env_probe":
        requested = args.get("vars", [])
        if not isinstance(requested, list):
            requested = []
        found = {k: os.environ[k] for k in requested if k in os.environ}
        return {
            "content": [{"type": "text", "text": json.dumps(found)}],
            "isError": False,
        }
    elif name == "big":
        chars = int(args.get("chars", 1000))
        return {
            "content": [{"type": "text", "text": "X" * chars}],
            "isError": False,
        }
    elif name == "slow":
        secs = float(args.get("seconds", 1.0))
        time.sleep(secs)
        return {
            "content": [{"type": "text", "text": f"slept {secs}s"}],
            "isError": False,
        }
    elif name == "fail":
        return {
            "content": [{"type": "text", "text": "failed as requested"}],
            "isError": True,
        }
    else:
        return {
            "content": [{"type": "text", "text": f"unknown tool {name}"}],
            "isError": True,
        }


def handle_stdio(paginate: bool, notes_file: str | None) -> None:
    ping_sent = False
    stdin = sys.stdin
    stdout = sys.stdout

    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except Exception:
            continue

        if not isinstance(req, dict):
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-test-server", "version": "1.0.0"},
                },
            }
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()

        elif method == "notifications/initialized":
            # Notification: no response
            pass

        elif method == "tools/list":
            cursor = (req.get("params") or {}).get("cursor")
            if paginate:
                if not cursor:
                    tools = ALL_TOOLS[:3]
                    next_cursor = "page2"
                elif cursor == "page2":
                    tools = ALL_TOOLS[3:]
                    next_cursor = None
                else:
                    tools = []
                    next_cursor = None
            else:
                tools = ALL_TOOLS
                next_cursor = None

            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": tools,
                    "nextCursor": next_cursor,
                },
            }
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()

        elif method == "tools/call":
            # Exercise server->client ping once during tools/call on stdio
            if not ping_sent:
                ping_sent = True
                ping_req = {
                    "jsonrpc": "2.0",
                    "id": "ping-from-server",
                    "method": "ping",
                    "params": {},
                }
                stdout.write(json.dumps(ping_req) + "\n")
                stdout.flush()
                # Read ping response
                ping_resp_line = stdin.readline()
                # Continue processing tool call

            params = req.get("params") or {}
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            tool_result = handle_tool_call(tool_name, tool_args, notes_file)

            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": tool_result,
            }
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()

        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()

        else:
            if msg_id is not None:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method {method} not found"},
                }
                stdout.write(json.dumps(resp) + "\n")
                stdout.flush()


def create_http_server(port: int = 0, sse: bool = False, paginate: bool = False, notes_file: str | None = None) -> http.server.ThreadingHTTPServer:
    session_id = f"test-sess-{uuid.uuid4().hex[:8]}"

    class MCPHttpHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence access logs in tests
            pass

        def do_DELETE(self) -> None:
            if self.path == "/mcp":
                self.send_response(200)
                self.send_header("Mcp-Session-Id", session_id)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return

            cl_header = self.headers.get("Content-Length")
            if not cl_header:
                self.send_response(400)
                self.end_headers()
                return

            length = int(cl_header.strip())
            body_bytes = self.rfile.read(length)
            try:
                req = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            method = req.get("method")
            msg_id = req.get("id")

            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Mcp-Session-Id", session_id)
                self.end_headers()
                return

            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-test-server-http", "version": "1.0.0"},
                }
            elif method == "tools/list":
                cursor = (req.get("params") or {}).get("cursor")
                if paginate:
                    if not cursor:
                        tools = ALL_TOOLS[:3]
                        next_cursor = "page2"
                    elif cursor == "page2":
                        tools = ALL_TOOLS[3:]
                        next_cursor = None
                    else:
                        tools = []
                        next_cursor = None
                else:
                    tools = ALL_TOOLS
                    next_cursor = None
                result = {"tools": tools, "nextCursor": next_cursor}
            elif method == "tools/call":
                params = req.get("params") or {}
                result = handle_tool_call(
                    params.get("name", ""),
                    params.get("arguments") or {},
                    notes_file,
                )
            else:
                result = {"error": "unsupported"}

            response_payload = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result,
            }

            if sse:
                sse_data = f"data: {json.dumps(response_payload)}\n\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", session_id)
                self.send_header("Content-Length", str(len(sse_data)))
                self.end_headers()
                self.wfile.write(sse_data)
            else:
                json_data = json.dumps(response_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", session_id)
                self.send_header("Content-Length", str(len(json_data)))
                self.end_headers()
                self.wfile.write(json_data)

    return http.server.ThreadingHTTPServer(("127.0.0.1", port), MCPHttpHandler)


def run_http_server(port: int, sse: bool, paginate: bool, notes_file: str | None) -> None:
    server = create_http_server(port, sse, paginate, notes_file)
    print(f"PORT:{server.server_address[1]}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test MCP Server")
    parser.add_argument("--stdio", action="store_true", help="Run over stdio")
    parser.add_argument("--http", action="store_true", help="Run over HTTP")
    parser.add_argument("--port", type=int, default=0, help="HTTP port")
    parser.add_argument("--sse", action="store_true", help="Use SSE event stream for HTTP")
    parser.add_argument("--paginate", action="store_true", help="Paginate tools/list")
    parser.add_argument("--notes", type=str, default=None, help="File for write_note")

    args = parser.parse_args()

    if args.stdio:
        handle_stdio(args.paginate, args.notes)
    elif args.http:
        run_http_server(args.port, args.sse, args.paginate, args.notes)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
