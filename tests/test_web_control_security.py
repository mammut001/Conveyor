from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers.job_queue import JobQueue
from web_control import WebControl


class WebControlSecurityTests(unittest.TestCase):
    def test_host_screen_capture_requires_opt_in_thumbnail_sharing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                codex_memory_root=Path(temp_dir),
                conveyor_desktop_upload_enabled=False,
            )
            control = object.__new__(WebControl)
            control.settings = settings
            disabled = control.request_host_screen()
            self.assertFalse(disabled["ok"])
            self.assertEqual(disabled["error"], "thumbnail_preview_disabled")

            settings.conveyor_desktop_upload_enabled = True
            response = {"ok": True, "request": {
                "request_id": "obs_test", "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
            }}
            with patch("desktop_observe_requests.create_observe_request", return_value=response) as create:
                result = control.request_host_screen()

            self.assertEqual(result["request"]["request_id"], "obs_test")
            self.assertTrue(create.call_args.kwargs["auto_upload_thumbnail"])
            self.assertFalse(create.call_args.kwargs["auto_delivery"])
            self.assertEqual(create.call_args.args[1].channel, "web")

    def test_computer_status_only_exposes_previews_from_web_console_captures(self):
        control = object.__new__(WebControl)
        control.settings = SimpleNamespace(conveyor_desktop_upload_enabled=True)
        host_request = {
            "request_id": "obs_host", "status": "completed", "auto_upload_thumbnail": True,
            "created_by_channel": "web", "user_request": "web-console-host-screen-preview",
        }
        uploads = [
            {"upload_id": "upload_other", "observe_request_id": "obs_other", "status": "completed",
             "result": {"thumbnail_path": "/private/other.png", "node_id": "other-node"}},
            {"upload_id": "upload_host", "observe_request_id": "obs_host", "status": "completed",
             "result": {"thumbnail_path": "/private/host.png", "node_id": "mac-node"}},
        ]
        with (
            patch("desktop_computer_requests.get_active_task", return_value=None),
            patch("desktop_computer_requests.arm_remaining_seconds", return_value=0),
            patch("desktop_computer_requests.is_direct_mode_active", return_value=False),
            patch("desktop_observe_requests.list_recent_observe_requests", return_value=[host_request]),
            patch("desktop_upload_requests.list_recent_upload_requests", side_effect=[uploads, uploads]),
            patch("desktop_upload_requests.ensure_upload_request_for_observe"),
        ):
            status = control.computer_status()

        self.assertEqual([item["artifact_id"] for item in status["screenshots"]], ["upload_host"])

    def test_expired_approval_cannot_execute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(codex_memory_root=Path(temp_dir))
            queue = JobQueue()
            queue.configure(settings, SimpleNamespace(current_job=None), recover=False)
            runner = SimpleNamespace(current_job=None)
            control = WebControl(settings, runner, queue)
            connection = control._connect()
            try:
                with connection:
                    connection.execute(
                        """INSERT INTO web_approvals
                           (id, job_id, action, status, created_at, expires_at)
                           VALUES ('expired', 'q1', 'apply', 'pending', 'now', ?)""",
                        (time.time() - 1,),
                    )
            finally:
                connection.close()

            result = asyncio.run(control.decide_approval("expired", True))
            self.assertEqual(result, {"id": "expired", "status": "expired"})

    def test_artifact_identifier_rejects_path_syntax(self):
        control = object.__new__(WebControl)
        self.assertIsNone(control.artifact_path("../secret.png"))
        self.assertIsNone(control.artifact_path("nested/file"))


if __name__ == "__main__":
    unittest.main()
