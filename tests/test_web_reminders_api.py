"""Tests for GET /api/reminders and POST /api/reminders/<id>/cancel."""
from __future__ import annotations

import asyncio
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from personal_tools.store import PersonalToolsStore

TOKEN = "t" * 40


def _settings(root: Path, **overrides):
    from conveyor.core.config import Settings

    base = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=42,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        routines_enabled=True,
    )
    return replace(base, **overrides)


class WebRemindersApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from conveyor.entrypoints.web_console import WebConsoleHandler, WebConsoleServer
        from conveyor.web.web_control import WebControl

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS queued_jobs "
            "(id TEXT, channel TEXT, operator_id TEXT, chat_id TEXT)"
        )
        conn.commit()
        conn.close()
        queue = mock.MagicMock()
        queue.list_jobs.return_value = []
        queue._db_path.return_value = self.root / "state" / "job_queue.sqlite3"
        control = WebControl(self.settings, runner=mock.MagicMock(), queue=queue)
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=control, loop=loop, token=TOKEN,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            thread.join(timeout=2)

        self.addCleanup(stop)
        self.store = PersonalToolsStore(self.settings)

    def call(self, method: str, path: str, body=None, *, authorized: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers: dict[str, str] = {}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_list_returns_only_pending_sorted_and_capped(self) -> None:
        base = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        owner = str(self.settings.telegram_allowed_user_id)
        # Create out of order + done + cancelled + another operator
        r_later = self.store.create_reminder(
            owner, "交房租", base + timedelta(hours=5), channel="telegram", chat_id=owner,
        )
        r_earliest = self.store.create_reminder(
            owner, "给妈妈打电话", base + timedelta(hours=1), channel="telegram", chat_id=owner,
        )
        r_cancelled = self.store.create_reminder(
            owner, "已取消的提醒", base + timedelta(hours=2), channel="telegram", chat_id=owner,
        )
        self.assertTrue(self.store.cancel_reminder(owner, r_cancelled.id))
        r_done = self.store.create_reminder(
            owner, "已完成的提醒", base + timedelta(hours=3), channel="telegram", chat_id=owner,
        )
        self.assertTrue(self.store.mark_reminder_done(r_done.id, base.isoformat()))
        self.store.create_reminder(
            "999", "别人的提醒", base + timedelta(minutes=30), channel="telegram", chat_id="999",
        )

        status, data = self.call("GET", "/api/reminders")
        self.assertEqual(status, 200, data)
        self.assertEqual(
            data["reminders"],
            [
                {
                    "id": r_earliest.id,
                    "text": "给妈妈打电话",
                    "due_at": r_earliest.due_at,
                    "channel": "telegram",
                },
                {
                    "id": r_later.id,
                    "text": "交房租",
                    "due_at": r_later.due_at,
                    "channel": "telegram",
                },
            ],
        )

    def test_list_returns_at_most_50_even_with_earlier_done_rows(self) -> None:
        base = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        owner = str(self.settings.telegram_allowed_user_id)
        for i in range(55):
            done = self.store.create_reminder(
                owner, f"done-{i}", base + timedelta(minutes=i), channel="telegram", chat_id=owner,
            )
            self.store.mark_reminder_done(done.id, base.isoformat())
        for i in range(52):
            self.store.create_reminder(
                owner, f"pending-{i:02d}", base + timedelta(days=1, minutes=i), channel="telegram", chat_id=owner,
            )

        status, data = self.call("GET", "/api/reminders")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["reminders"]), 50)
        self.assertEqual(data["reminders"][0]["text"], "pending-00")
        self.assertEqual(data["reminders"][-1]["text"], "pending-49")

    def test_falsy_telegram_allowed_user_id_returns_empty_list(self) -> None:
        self.server.control.settings = _settings(self.root, telegram_allowed_user_id=0)
        status, data = self.call("GET", "/api/reminders")
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"reminders": []})

        cancel_status, cancel_data = self.call("POST", "/api/reminders/1/cancel", {})
        self.assertEqual(cancel_status, 404)
        self.assertEqual(cancel_data, {"error": "reminder not found"})

    def test_cancel_removes_from_list_and_cancel_twice_returns_404(self) -> None:
        base = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        owner = str(self.settings.telegram_allowed_user_id)
        rem = self.store.create_reminder(owner, "喝水", base, channel="telegram", chat_id=owner)

        status, data = self.call("POST", f"/api/reminders/{rem.id}/cancel", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"ok": True})

        list_status, list_data = self.call("GET", "/api/reminders")
        self.assertEqual(list_status, 200)
        self.assertEqual(list_data, {"reminders": []})

        second_status, second_data = self.call("POST", f"/api/reminders/{rem.id}/cancel", {})
        self.assertEqual(second_status, 404, second_data)
        self.assertEqual(second_data, {"error": "reminder not found"})

    def test_cancel_bad_id_returns_400(self) -> None:
        for bad in ("abc", "-1"):
            with self.subTest(bad=bad):
                status, _ = self.call("POST", f"/api/reminders/{bad}/cancel", {})
                self.assertEqual(status, 400)

    def test_unauthenticated_requests_return_401(self) -> None:
        status_get, _ = self.call("GET", "/api/reminders", authorized=False)
        self.assertEqual(status_get, 401)
        status_post, _ = self.call("POST", "/api/reminders/1/cancel", {}, authorized=False)
        self.assertEqual(status_post, 401)

    def test_not_gated_behind_routines_enabled(self) -> None:
        self.server.control.settings = _settings(self.root, routines_enabled=False)
        base = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        owner = str(self.settings.telegram_allowed_user_id)
        rem = self.store.create_reminder(owner, "开会", base, channel="telegram", chat_id=owner)

        status, data = self.call("GET", "/api/reminders")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["reminders"]), 1)

        c_status, c_data = self.call("POST", f"/api/reminders/{rem.id}/cancel", {})
        self.assertEqual(c_status, 200, c_data)
        self.assertEqual(c_data, {"ok": True})
