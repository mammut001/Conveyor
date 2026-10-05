"""The VPS chat window talks to the local web console and nowhere else."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desktop_chat_window import (
    enable_system_gtk,
    local_screenshot_file,
    post_chat,
    read_chat_events,
    screenshot_id_from_reply,
    web_chat_url,
)
from handlers.tools.executors import _format_loop_result


class DesktopChatWindowTest(unittest.TestCase):
    def test_url_uses_the_configured_web_port(self) -> None:
        settings = SimpleNamespace(conveyor_web_host="127.0.0.1", conveyor_web_port=18787)
        self.assertEqual(web_chat_url(settings), "http://127.0.0.1:18787/api/chat")

    def test_sse_keeps_the_assistant_text(self) -> None:
        raw = (
            "event: session\ndata: {\"session_id\":\"web-1\"}\n\n"
            "event: message\ndata: {\"text\":\"🖥 Computer Use 任务 ctsk_1\"}\n\n"
            "event: done\ndata: {\"outcome\":\"answered\"}\n\n"
        )
        outcome, text = read_chat_events(raw)
        self.assertEqual(outcome, "answered")
        self.assertIn("ctsk_1", text)

    def test_system_gtk_path_is_appended(self) -> None:
        extra = "/usr/lib/python3/dist-packages"
        saved = list(sys.path)
        try:
            sys.path = [item for item in sys.path if item != extra]
            with mock.patch("desktop_chat_window.os.path.isdir", return_value=True):
                enable_system_gtk()
                self.assertEqual(sys.path[-1], extra)
                enable_system_gtk()
                self.assertEqual(sys.path.count(extra), 1)
        finally:
            sys.path[:] = saved

    def test_reply_names_a_local_screenshot(self) -> None:
        shot = "20261005T120000Z-cua-abcd1234"
        text = _format_loop_result(None, {
            "ok": True,
            "status": "done",
            "task_id": "ctsk_1",
            "steps_used": 2,
            "summary": "clicked",
            "screenshot_id": shot,
        })
        self.assertEqual(screenshot_id_from_reply(text), shot)
        self.assertNotIn("base64", text)
        self.assertIsNone(screenshot_id_from_reply("你好"))
        self.assertEqual(
            screenshot_id_from_reply(f"Last Screenshot: {shot} (hash: ab)"),
            shot,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / f"{shot}.png").write_bytes(b"png")
            settings = SimpleNamespace(
                conveyor_desktop_screenshot_dir=str(root),
                codex_memory_root=root,
            )
            found = local_screenshot_file(settings, shot)
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.name, f"{shot}.png")
            self.assertTrue(found.is_file())
            self.assertIsNone(local_screenshot_file(settings, "../secret"))
            self.assertIsNone(local_screenshot_file(settings, "missing"))

    def test_post_without_a_token_does_not_call_the_network(self) -> None:
        code, body = post_chat(SimpleNamespace(conveyor_web_token=""), "打开计算器", timeout=1)
        self.assertEqual(code, 0)
        self.assertEqual(body, "")
