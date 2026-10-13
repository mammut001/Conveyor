#!/usr/bin/env python3
"""Tiny deliberately faulty MCP stdio server for lifecycle and safety tests."""
import json
import os
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
TOOLS = [{"name": "echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "big", "inputSchema": {"type": "object", "properties": {"size": {"type": "integer"}}}},
         {"name": "hang", "inputSchema": {"type": "object", "properties": {}}}]

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    method = req.get("method")
    if method == "notifications/initialized":
        continue
    if method == "tools/list" and MODE == "crash_once":
        marker = os.environ["MCP_CRASH_MARKER"]
        if not os.path.exists(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write("crashed")
            sys.exit(17)
    if method == "tools/list" and MODE == "malformed":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        sys.exit(0)
    if method == "tools/call" and req.get("params", {}).get("name") == "hang":
        time.sleep(8)
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        args = req.get("params", {}).get("arguments", {})
        tool = req.get("params", {}).get("name")
        result = {"content": [{"type": "text", "text": (
            "z" * int(args.get("size", 0)) if tool == "big" else str(args.get("text", "ok"))
        )}]}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": result}) + "\n")
    sys.stdout.flush()
