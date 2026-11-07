"""MCP: a config typo or a slow HTTP server must not take the chat down."""
from __future__ import annotations

import asyncio
import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_client.config import load_mcp_servers
from mcp_client.manager import MCPManager


class McpConfigTypoTest(unittest.TestCase):
    def test_non_table_server_entry_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "mcp_servers.toml"
            path.write_text('[servers]\nnotes = "oops"\n')
            servers, _err = load_mcp_servers(path)
        self.assertIn("notes", servers)
        self.assertTrue(servers["notes"].validation_error)


class _Drip(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            for _ in range(40):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_a):
        pass


class McpHttpDeadlineTest(unittest.TestCase):
    def test_dripping_body_is_cut_at_the_timeout(self) -> None:
        from mcp_client.client import HttpSession, MCPTimeoutError
        from mcp_client.types import ServerConfig
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cfg = ServerConfig(name="drip", transport="http",
                               url=f"http://127.0.0.1:{server.server_address[1]}/mcp", timeout_seconds=1)
            session = HttpSession(cfg)
            started = time.monotonic()
            with self.assertRaises(MCPTimeoutError):
                session._post_sync({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            # The old per-read timeout let this run for the full 10 s drip.
            self.assertLess(time.monotonic() - started, 3.0)
        finally:
            server.shutdown()


class McpRefreshDedupTest(unittest.IsolatedAsyncioTestCase):
    """Concurrent ensure_fresh calls must share one in-flight refresh per server."""

    def setUp(self) -> None:
        from tests.test_mcp import _settings
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        # A single enabled server so every ensure_fresh sees it as stale.
        config = {"servers": {"notes": {"transport": "http", "url": "http://127.0.0.1:9/mcp", "allow_tools": ["*"]}}}
        self.settings.mcp_config_path.write_text(json.dumps(config), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_concurrent_ensure_fresh_refreshes_server_once(self) -> None:
        mgr = MCPManager()
        calls = 0

        async def slow_refresh(settings, server_name):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.2)
            return {"name": server_name}, 200

        with patch.object(mgr, "refresh_server", new=slow_refresh):
            await asyncio.gather(*(mgr.ensure_fresh(self.settings) for _ in range(5)))
        # All five chats shared one refresh instead of spawning five.
        self.assertEqual(calls, 1)
        # The in-flight entry is dropped once the refresh completes.
        self.assertEqual(mgr._refresh_inflight, {})

    async def test_cancelling_one_waiter_does_not_cancel_shared_refresh(self) -> None:
        mgr = MCPManager()
        calls = 0
        finished = asyncio.Event()

        async def slow_refresh(settings, server_name):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.3)
            finished.set()
            return {"name": server_name}, 200

        with patch.object(mgr, "refresh_server", new=slow_refresh):
            tasks = [asyncio.ensure_future(mgr.ensure_fresh(self.settings)) for _ in range(3)]
            await asyncio.sleep(0.05)  # let all three attach to the same in-flight refresh
            tasks[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await tasks[0]
            # The shield kept the shared refresh alive: the others still complete.
            await asyncio.gather(tasks[1], tasks[2])
        self.assertEqual(calls, 1)
        self.assertTrue(finished.is_set())
        self.assertEqual(mgr._refresh_inflight, {})


if __name__ == "__main__":
    unittest.main()
