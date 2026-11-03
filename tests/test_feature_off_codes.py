"""409 answers for switched-off features carry a stable ``code`` next to the old ``error`` text."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers.job_queue import JobQueue
from web_console import FEATURE_OFF_ERRORS, WebConsoleHandler, WebConsoleServer
from web_control import WebControl

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"

# The web console still matches on these exact English texts.
EXPECTED_ERRORS = {
    "agents_disabled": "agents are disabled (set CONVEYOR_AGENTS_ENABLED=true)",
    "memory_disabled": "memory is disabled (set CONVEYOR_MEMORY_V2=1 or CONVEYOR_LONG_TERM_MEMORY=true)",
    "memory_v2_required": "memory editing requires CONVEYOR_MEMORY_V2=1",
    "skills_disabled": "skills are disabled (set CONVEYOR_SKILLS_ENABLED=true)",
    "mcp_disabled": "MCP connectors are disabled (set CONVEYOR_MCP_ENABLED=true)",
    "approval_inbox_disabled": "approval inbox is disabled (set CONVEYOR_APPROVAL_INBOX_ENABLED=true)",
    "routines_disabled": "routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)",
    "webhooks_disabled": "webhooks are disabled (set CONVEYOR_WEBHOOKS_ENABLED=true and CONVEYOR_ROUTINES_ENABLED=true)",
    "chat_disabled": "chat tier is disabled (set CONVEYOR_CHAT_MODE and CONVEYOR_CHAT_*)",
}


class FeatureOffCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        td = Path(cls.temp_dir.name)
        cls.settings = SimpleNamespace(codex_memory_root=td, codex_task_root=td)
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        cls.control = WebControl(cls.settings, runner=None, queue=cls.queue)
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=cls.control, loop=cls.loop, token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def setUp(self):
        # Every feature off; chat has no model configured.
        for name in (
            "agents_enabled", "long_term_memory_enabled", "memory_v2_enabled", "skills_enabled",
            "mcp_enabled", "approval_inbox_enabled", "routines_enabled", "webhooks_enabled",
        ):
            setattr(self.settings, name, False)
        self.settings.chat_mode = "off"
        self.settings.conveyor_chat_mode = "off"

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={
            "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
        })
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def assert_feature_off(self, code, method, path, body=None):
        with self.subTest(method=method, path=path):
            status, payload = self.request(method, path, body)
            self.assertEqual(status, 409)
            self.assertEqual(payload, {"error": EXPECTED_ERRORS[code], "code": code})

    def test_error_texts_are_unchanged(self):
        self.assertEqual(FEATURE_OFF_ERRORS, EXPECTED_ERRORS)

    def test_switched_off_features_answer_with_code(self):
        cases = [
            ("agents_disabled", "POST", "/api/agents", {"name": "x"}),
            ("agents_disabled", "PUT", "/api/agents/x", {"name": "x"}),
            ("agents_disabled", "DELETE", "/api/agents/x", None),
            ("memory_disabled", "GET", "/api/memory", None),
            ("skills_disabled", "GET", "/api/skills", None),
            ("mcp_disabled", "GET", "/api/mcp/servers", None),
            ("approval_inbox_disabled", "GET", "/api/approval-inbox", None),
            ("approval_inbox_disabled", "POST", "/api/approval-inbox/any-id/approve", {}),
            ("routines_disabled", "GET", "/api/routines", None),
            ("routines_disabled", "GET", "/api/inbox", None),
            ("routines_disabled", "POST", "/api/routines", {"name": "x"}),
            ("routines_disabled", "DELETE", "/api/routines/1", None),
        ]
        for code, method, path, body in cases:
            self.assert_feature_off(code, method, path, body)

    def test_chat_tier_off(self):
        with patch.dict(os.environ, {"CONVEYOR_NIGHTLY_CONTROL": "0"}):
            self.assert_feature_off("chat_disabled", "POST", "/api/chat", {"message": "hello"})

    def test_memory_editing_needs_memory_v2(self):
        self.settings.long_term_memory_enabled = True
        self.assert_feature_off("memory_v2_required", "PUT", "/api/memory/1", {"text": "x"})

    def test_webhooks_off_while_routines_on(self):
        self.settings.routines_enabled = True
        self.assert_feature_off("webhooks_disabled", "POST", "/api/routines/1/hook", {})
        self.assert_feature_off("webhooks_disabled", "DELETE", "/api/routines/1/hook")


if __name__ == "__main__":
    unittest.main()
