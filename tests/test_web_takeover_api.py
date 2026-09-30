from __future__ import annotations

import asyncio
import http.client
import json
import threading
import unittest

from web_console_takeover import TakeoverWebConsoleHandler, TakeoverWebConsoleServer

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class FakeTakeover:
    def __init__(self):
        self.state = {
            "takeover": None,
            "privacy_mode": False,
            "closing": None,
            "transport": {"phase": "idle", "running": False, "ready": False},
        }

    def status(self):
        return self.state

    def start(self, payload):
        self.state = {
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
        cls.server = TakeoverWebConsoleServer(
            ("127.0.0.1", 0),
            TakeoverWebConsoleHandler,
            control=object(),
            loop=cls.loop,
            token=TOKEN,
            takeover=FakeTakeover(),
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
        status, started = self.request("POST", "/api/takeover/start", {"reason": "operator_requested"})
        self.assertEqual(status, 202)
        self.assertTrue(started["privacy_mode"])
        self.assertEqual(started["takeover"]["state"], "waiting_for_human")

        status, active = self.request("POST", "/api/takeover/activate", {"session_id": "takeover-1"})
        self.assertEqual(status, 200)
        self.assertEqual(active["takeover"]["state"], "human_active")
        self.assertTrue(active["transport"]["ready"])

        status, closing = self.request("POST", "/api/takeover/complete", {"session_id": "takeover-1"})
        self.assertEqual(status, 202)
        self.assertEqual(closing["closing"], "complete")

    def test_invalid_session_is_bad_request(self):
        status, payload = self.request("POST", "/api/takeover/cancel", {"session_id": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn("invalid state", payload["error"])


if __name__ == "__main__":
    unittest.main()
