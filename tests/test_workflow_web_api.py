"""Authenticated HTTP integration tests for opt-in durable workflows."""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from web_console import WebConsoleHandler, WebConsoleServer


_TOKEN = "workflow-api-test-token-0123456789"


class WorkflowApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = SimpleNamespace(
            codex_memory_root=Path(cls.tmp.name), workflows_enabled=False,
            live_screen_enabled=False,
        )
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=SimpleNamespace(settings=cls.settings), loop=cls.loop, token=_TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=3)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=3)
        cls.loop.close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + _TOKEN
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        status = response.status
        data = json.loads(response.read())
        conn.close()
        return status, data

    def test_00_auth_precedes_store_and_disabled_is_side_effect_free(self):
        self.settings.workflows_enabled = False
        self.assertEqual(self.req("GET", "/api/workflows", authorized=False)[0], 401)
        self.assertEqual(self.req("POST", "/api/workflows", body={}, authorized=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/workflows")[0], 409)
        self.assertEqual(self.req("POST", "/api/workflows", body={})[0], 409)
        self.assertFalse((self.settings.codex_memory_root / "state" / "workflows.sqlite3").exists())

    def test_01_http_create_cas_and_replay(self):
        self.settings.workflows_enabled = True
        body = {
            "title": "Safe draft-only example",
            "steps": [
                {"kind": "browser", "label": "Prepare a mock form"},
                {"kind": "human", "label": "Human-only authentication"},
                {"kind": "submission", "label": "Human reviews final submission"},
            ],
            "request_id": "api-create-0001",
        }
        status, created = self.req("POST", "/api/workflows", body)
        self.assertEqual(status, 201)
        self.assertEqual(created["revision"], 0)
        self.assertEqual(self.req("POST", "/api/workflows", body)[1]["id"], created["id"])
        self.assertEqual(self.req("GET", "/api/workflows")[1]["items"][0]["id"], created["id"])
        self.assertEqual(self.req("GET", "/api/workflows/" + created["id"])[0], 200)
        self.assertEqual(self.req("GET", "/api/workflows/not-found")[0], 404)
        endpoint = "/api/workflows/" + created["id"] + "/action"
        move = {"action": "begin", "expected_revision": 0, "request_id": "api-begin-0001"}
        status, started = self.req("POST", endpoint, move)
        self.assertEqual(status, 200)
        self.assertEqual(started["revision"], 1)
        self.assertEqual(self.req("POST", endpoint, move)[1]["replayed"], True)
        self.assertEqual(self.req("POST", endpoint, {**move, "request_id": "api-begin-0002"})[0], 409)
        status, finished = self.req("POST", endpoint, {
            "action": "complete", "expected_revision": 1, "request_id": "api-done-0001",
        })
        self.assertEqual(status, 200)
        self.assertEqual(finished["state"], "waiting_for_human")
        self.assertEqual(self.req("POST", endpoint, {
            "action": "begin", "expected_revision": 2, "request_id": "api-bypass-0001",
        })[0], 409)
        status, confirmed = self.req("POST", endpoint, {
            "action": "confirm_human", "expected_revision": 2, "request_id": "api-verify-0001",
        })
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["steps"][-1]["state"], "waiting_for_human")
        self.assertEqual(self.req("POST", endpoint, {
            "action": "complete", "expected_revision": 3, "request_id": "api-submit-0001",
        })[0], 409)
        self.assertEqual(self.req("POST", endpoint, {
            "action": "confirm_human", "expected_revision": 3, "request_id": "api-human-0002",
        })[1]["state"], "completed")

    def test_02_no_sensitive_values_accepted_in_labels(self):
        self.settings.workflows_enabled = True
        status, _ = self.req("POST", "/api/workflows", {
            "title": "Bad\nTitle",
            "steps": [{"kind": "work", "label": "Step"}],
            "request_id": "bad-title-00001",
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
