"""The VPS chat window talks to the local web console and nowhere else."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from desktop_chat_window import post_chat, read_chat_events, web_chat_url


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

    def test_post_without_a_token_does_not_call_the_network(self) -> None:
        code, body = post_chat(SimpleNamespace(conveyor_web_token=""), "打开计算器", timeout=1)
        self.assertEqual(code, 0)
        self.assertEqual(body, "")
