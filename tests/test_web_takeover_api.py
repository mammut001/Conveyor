from __future__ import annotations

import asyncio
import http.client
import json
import threading
import unittest

from web_console_takeover import TakeoverWebConsoleHandler, TakeoverWebConsoleServer

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class FakeTakeover:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.state = {
            "enabled": enabled,
            "takeover": None,
            "privacy_mode": False,
            "closing": None,
            "transport": {"phase": "idle", "running": False, "ready": False},
        }

    def status(self):
        if not self.enabled:
            return {
                "enabled": False,
                "takeover": None,
                "privacy_mode": False,
                "closing": None,
                "transport_allowed": False,
                "transport": None,
                "message": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)",
            }
        return self.state

    def start(self, payload):
        if not self.enabled:
            raise RuntimeError("Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)")
        self.state = {
            "enabled": True,
            "takeover": {
                "id": "takeover-1", "state": "waiting_for_human",
                "reason": payload.get("reason") or "operator_requested",
                "remaining_seconds": 300,
            },
            "privacy_mode": True,
            "closing": None,
            "transport": {"phase": "starting", "running": False, "ready": False},
        }
        return self.state

    def activate(self, session_id):
        if session_id != "takeover-1":
            raise ValueError("takeover session not found or invalid state")
        self.state["takeover"]["state"] = "human_active"
        self.state["transport"] = {
            "phase": "ready", "running": True, "ready": True,
            "url": "https://vps.tailnet.ts.net:8443/vnc.html",
        }
        return self.state

    def close(self, session_id, action):
        if session_id != "takeover-1":
            raise ValueError("takeover session not found or invalid state")
        self.state["closing"] = action
        return self.state


class WebTakeoverApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.takeover = FakeTakeover(enabled=True)
        cls.server = TakeoverWebConsoleServer(
            ("127.0.0.1", 0),
            TakeoverWebConsoleHandler,
            control=object(),
            loop=cls.loop,
            token=TOKEN,
            takeover=cls.takeover,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2); cls.server_thread.join(timeout=2)

    def request(self, method, path, body=None, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, payload

    def test_takeover_routes_require_web_bearer_auth(self):
        self.assertEqual(self.request("GET", "/api/takeover/status", authorized=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/takeover/start", {}, authorized=False)[0], 401)

    def test_start_activate_and_close_flow(self):
        status, initial = self.request("GET", "/api/takeover/status")
        self.assertEqual(status, 200)
        self.assertTrue(initial["enabled"])

        status, started = self.request("POST", "/api/takeover/start", {"reason": "operator_requested"})
        self.assertEqual(status, 202)
        self.assertTrue(started["enabled"])
        self.assertTrue(started["privacy_mode"])
        self.assertEqual(started["takeover"]["state"], "waiting_for_human")

        status, active = self.request("POST", "/api/takeover/activate", {"session_id": "takeover-1"})
        self.assertEqual(status, 200)
        self.assertTrue(active["enabled"])
        self.assertEqual(active["takeover"]["state"], "human_active")
        self.assertTrue(active["transport"]["ready"])

        status, closing = self.request("POST", "/api/takeover/complete", {"session_id": "takeover-1"})
        self.assertEqual(status, 202)
        self.assertTrue(closing["enabled"])
        self.assertEqual(closing["closing"], "complete")

    def test_invalid_session_is_bad_request(self):
        status, payload = self.request("POST", "/api/takeover/cancel", {"session_id": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn("invalid state", payload["error"])

    def test_takeover_routes_when_disabled(self):
        try:
            self.takeover.enabled = False
            status, _ = self.request("GET", "/api/takeover/status", authorized=False)
            self.assertEqual(status, 401)

            status, payload = self.request("GET", "/api/takeover/status", authorized=True)
            self.assertEqual(status, 200)
            self.assertEqual(
                payload,
                {
                    "enabled": False,
                    "takeover": None,
                    "privacy_mode": False,
                    "closing": None,
                    "transport_allowed": False,
                    "transport": None,
                    "message": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)",
                },
            )

            for path in ("/api/takeover/start", "/api/takeover/activate", "/api/takeover/complete", "/api/takeover/cancel"):
                with self.subTest(endpoint=path, auth=False):
                    status, _ = self.request("POST", path, {"reason": "operator_requested", "session_id": "takeover-1"}, authorized=False)
                    self.assertEqual(status, 401)

                with self.subTest(endpoint=path, auth=True):
                    status, body = self.request("POST", path, {"reason": "operator_requested", "session_id": "takeover-1"}, authorized=True)
                    self.assertEqual(status, 403)
                    self.assertEqual(body, {"error": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)"})
        finally:
            self.takeover.enabled = True

    def test_disabled_flag_creates_no_lease_or_gate_files(self):
        import tempfile
        from types import SimpleNamespace
        from human_takeover import HumanTakeoverStore
        from web_takeover import WebTakeover, close_request_path, transport_gate_path

        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(
                codex_memory_root=root,
                conveyor_computer_max_seconds=1,
                conveyor_takeover_enabled=False,
            )
            real_takeover = WebTakeover(settings)
            server = TakeoverWebConsoleServer(
                ("127.0.0.1", 0),
                TakeoverWebConsoleHandler,
                control=object(),
                loop=self.loop,
                token=TOKEN,
                takeover=real_takeover,
            )
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            port = server.server_address[1]
            try:
                def req(method, path, body=None, authorized=True):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    headers = {"Content-Type": "application/json"}
                    if authorized:
                        headers["Authorization"] = f"Bearer {TOKEN}"
                    data = json.dumps(body).encode() if body is not None else None
                    conn.request(method, path, body=data, headers=headers)
                    res = conn.getresponse()
                    data = json.loads(res.read() or b"{}")
                    conn.close()
                    return res.status, data

                st, body = req("GET", "/api/takeover/status")
                self.assertEqual(st, 200)
                self.assertFalse(body["enabled"])

                for path in ("/api/takeover/start", "/api/takeover/activate", "/api/takeover/complete", "/api/takeover/cancel"):
                    st, body = req("POST", path, {"reason": "operator_requested", "session_id": "test"})
                    self.assertEqual(st, 403)
                    self.assertEqual(body, {"error": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)"})

                self.assertIsNone(HumanTakeoverStore(settings).current())
                self.assertFalse(transport_gate_path(settings).exists())
                self.assertFalse(close_request_path(settings).exists())
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
