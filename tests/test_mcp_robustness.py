"""MCP: a config typo or a slow HTTP server must not take the chat down."""
from __future__ import annotations

import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp_client.config import load_mcp_servers


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

if __name__ == "__main__":
    unittest.main()
