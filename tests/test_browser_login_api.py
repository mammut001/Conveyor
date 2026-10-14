"""Authenticated login-management API: masks secrets and disappears when OFF."""
from __future__ import annotations
import asyncio
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from web_console import WebConsoleHandler, WebConsoleServer
from console_secrets import path_for

TOKEN = "login-api-test-token-long-long-long"


class FakeControl:
    def __init__(self, settings):
        self.settings = settings


class LoginAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = SimpleNamespace(
            codex_memory_root=Path(cls.tmp.name),
            approval_inbox_enabled=False, approval_relay_enabled=False,
            routines_enabled=False, mcp_enabled=False, skills_enabled=False,
            long_term_memory_enabled=False, chat_tools_network_allow=(),
        )
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()
        cls.server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler,
                                      control=FakeControl(cls.settings),
                                      loop=cls.loop, token=TOKEN)
        cls.web_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.web_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.web_thread.join(timeout=3)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def request(self, method, url, body=None, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + TOKEN
        conn.request(method, url, body=json.dumps(body).encode() if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        result = (resp.status, json.loads(resp.read()))
        conn.close()
        return result

    def test_flag_off_is_original_404(self):
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_AUTOFILL_LOGIN": "0"}):
            for method, path, payload in (
                ("GET", "/api/browser/logins", None),
                ("POST", "/api/browser/logins", {}),
                ("DELETE", "/api/browser/logins?domain=example.org", None),
            ):
                self.assertEqual(self.request(method, path, payload),
                                 (404, {"error": "not found"}))
        self.assertFalse(path_for(self.settings).exists())

    def test_authenticated_write_only_crud(self):
        env = {
            "CONVEYOR_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
            "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
            "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
            "CONVEYOR_TAKEOVER_ENABLED": "1",
            "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
            "CONVEYOR_BROWSER_AUTOFILL_LOGIN": "1",
            "CONVEYOR_BROWSER_AUTH_DOMAINS": "site.example.org",
        }
        with patch.dict(os.environ, env):
            domain = "site.example.org"
            secret = "sUp3r-P@ssw0rd"
            path = "/api/browser/logins"
            payload = {"domain": domain, "username": "alice", "password": secret}
            self.assertEqual(self.request("POST", path, payload, authorized=False)[0], 401)
            code, created = self.request("POST", path, payload)
            self.assertEqual(code, 201)
            self.assertEqual(created, {"domain": "https://site.example.org",
                                       "username": "alice", "masked": True})
            status, listed = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(listed["logins"], [created])
            self.assertNotIn(secret, json.dumps(listed))
            self.assertNotIn(secret, json.dumps(created))
            self.assertEqual(self.request("DELETE", path + "?domain=" + quote(domain))[0], 200)
            self.assertEqual(self.request("GET", path), (200, {"logins": []}))
