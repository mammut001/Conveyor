from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings, load_runtime_settings, load_settings
from scripts.telegram_api import send_message
from web_console import MAX_BODY_BYTES, WebConsoleHandler, WebConsoleServer


TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class FakeJob:
    id = "q-web-only"


DUMMY_SETTINGS = Settings(
    telegram_bot_token="",
    telegram_allowed_user_id=0,
    codex_workspace_root=Path("/tmp"),
    codex_bin="codex",
    codex_task_root=Path("/tmp"),
    codex_model=None,
    codex_timeout_seconds=3600,
    telegram_progress_seconds=3,
    codex_retry_429_delays_seconds=(300, 900, 1800),
    codex_memory_root=Path("/tmp"),
    user_timezone="UTC",
)


class FakeControl:
    runner = object()
    settings = DUMMY_SETTINGS

    def system_status(self):
        return {"uptime_seconds": 1, "queue": {"depth": 0}}

    def list_jobs(self, _limit=100):
        return []

    def get_job(self, _job_id):
        return None

    def resolve_session_identity(self, session_id):
        return ("web", "web-console", session_id) if session_id else None


async def fake_submit(*_args, **_kwargs):
    return True, "queued", FakeJob()


class WebOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=FakeControl(),
            loop=cls.loop,
            token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls.loop.close()

    def test_a_load_runtime_settings_without_telegram(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ws = tdp / "workspace"
            tasks = tdp / "tasks"
            mem = tdp / "memory"
            ws.mkdir()
            tasks.mkdir()
            mem.mkdir()
            env_file = tdp / "test.env"
            env_file.write_text(
                f"CODEX_WORKSPACE_ROOT={ws}\n"
                f"CODEX_TASK_ROOT={tasks}\n"
                f"CODEX_MEMORY_ROOT={mem}\n"
            )

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
                os.environ.pop("CONVEYOR_ENV_FILE", None)
                os.environ.pop("CODEX_WORKSPACE_ROOT", None)
                os.environ.pop("CODEX_TASK_ROOT", None)
                os.environ.pop("CODEX_MEMORY_ROOT", None)

                # load_runtime_settings succeeds without Telegram credentials
                settings = load_runtime_settings(env_file=env_file)
                self.assertEqual(settings.telegram_bot_token, "")
                self.assertEqual(settings.telegram_allowed_user_id, 0)
                self.assertEqual(settings.codex_workspace_root, ws.resolve())

                # load_settings fails with missing TELEGRAM_BOT_TOKEN
                with self.assertRaises(RuntimeError) as ctx:
                    load_settings(env_file=env_file)
                self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

                # If token is supplied but allowed user id is missing, load_settings still fails
                os.environ["TELEGRAM_BOT_TOKEN"] = "some-token"
                with self.assertRaises(RuntimeError) as ctx:
                    load_settings(env_file=env_file)
                self.assertIn("TELEGRAM_ALLOWED_USER_ID", str(ctx.exception))

    def test_b_keepalive_socket_unauthorized_post_then_authorized_request(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        # 1. Unauthorized POST with a JSON body
        body1 = json.dumps({"prompt": "unauthorized task", "session_id": "web-a"}).encode("utf-8")
        conn.request(
            "POST",
            "/api/tasks",
            body=body1,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body1)),
            },
        )
        resp1 = conn.getresponse()
        self.assertEqual(resp1.status, 401)
        resp1_data = resp1.read()
        self.assertIn(b"unauthorized", resp1_data)
        # Connection should remain open (no Connection: close)
        self.assertNotEqual(resp1.getheader("Connection", "").lower(), "close")

        # 2. Authorized request on the SAME connection (body bytes must have been drained)
        with patch("web_console.submit_codex_job", fake_submit):
            body2 = json.dumps({"prompt": "authorized task", "session_id": "web-a"}).encode("utf-8")
            conn.request(
                "POST",
                "/api/tasks",
                body=body2,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body2)),
                },
            )
            resp2 = conn.getresponse()
            self.assertEqual(resp2.status, 202)
            resp2_data = json.loads(resp2.read() or b"{}")
            self.assertTrue(resp2_data.get("ok"))
            self.assertEqual(resp2_data.get("job_id"), "q-web-only")
        conn.close()

    def test_c_unauthorized_post_oversized_closes_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        oversized = MAX_BODY_BYTES + 1024
        # We advertise an oversized body. Server should reject without reading and close connection.
        conn.putrequest("POST", "/api/tasks")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(oversized))
        conn.endheaders()

        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        self.assertEqual(resp.getheader("Connection", "").lower(), "close")
        resp.read()

        # Subsequent read on the socket must return EOF
        if conn.sock:
            self.assertEqual(conn.sock.recv(1024), b"")
        conn.close()

    def test_c_authorized_body_drain_and_close_branches(self):
        # Invalid Content-Length string sets close_connection
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/tasks")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "not-a-number")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection", "").lower(), "close")
        resp.read()
        conn.close()

        # Transfer-Encoding sets close_connection
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/tasks")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection", "").lower(), "close")
        resp.read()
        conn.close()

        # Oversized body (> MAX_BODY_BYTES) sets close_connection
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/tasks")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY_BYTES + 100))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection", "").lower(), "close")
        resp.read()
        conn.close()

        # Zero body length is a 400 error but does not close connection (0 bytes is clean)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/tasks")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "0")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertNotEqual(resp.getheader("Connection", "").lower(), "close")
        resp.read()
        conn.close()

    def test_d_telegram_api_empty_token_raises(self):
        settings = Settings(
            telegram_bot_token="",
            telegram_allowed_user_id=12345,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp"),
            codex_model=None,
            codex_timeout_seconds=3600,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(300, 900, 1800),
            codex_memory_root=Path("/tmp"),
            user_timezone="UTC",
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(RuntimeError) as ctx:
                send_message(settings, "test message")
            self.assertEqual(
                str(ctx.exception),
                "Telegram is not configured (TELEGRAM_BOT_TOKEN is empty)",
            )
            mock_urlopen.assert_not_called()

        from personal_tools.briefing import _send_briefing
        with self.assertRaises(RuntimeError) as ctx:
            _send_briefing("telegram", "12345", "test briefing", settings)
        self.assertEqual(
            str(ctx.exception),
            "Telegram is not configured (TELEGRAM_BOT_TOKEN is empty)",
        )


if __name__ == "__main__":
    unittest.main()
