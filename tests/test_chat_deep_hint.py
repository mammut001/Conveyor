"""The chat tier's "dig deeper" line: plain Chinese on web, never a command nobody can run.

QA r6 N12: a 提问 answer ended with 「发 /deep 让 Codex 深入查证。」 on the web
console, where there is no slash command and Codex was not even installed. The
line is now channel-aware and only appears when an executor can actually run.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from channel.types import InboundMessage
from handlers.chat import Checked, deep_hint, finalize


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        skills_enabled=False,
        chat_tools_enabled=False,
        web_search_backend="disabled",
        chat_history_turns=5,
        chat_vision=False,
        codex_memory_root=Path(tempfile.gettempdir()),
    )


def _payload(workers: list[dict], enabled: bool = True) -> dict:
    return {"enabled": enabled, "install_enabled": False, "workers": workers, "active": None}


READY_WORKER = {
    "id": "pi", "enabled": True, "installed": True, "runnable": True,
    "auth": "ok", "quota": {"state": "available"},
}
NOT_INSTALLED_WORKER = {
    "id": "codex", "enabled": True, "installed": False, "runnable": True,
    "auth": "unknown", "quota": {"state": "available"},
}


def _unverified_answer(deep: str) -> str:
    return finalize(
        Checked(body="大约 6 万美元。", confidence="medium", removed_links=0, escalate=False, reason=""),
        time_sensitive_unverified=True, deep=deep,
    )


def _low_confidence_answer(deep: str) -> str:
    return finalize(
        Checked(body="大概是 1998 年。", confidence="low", removed_links=0, escalate=False, reason=""),
        time_sensitive_unverified=False, deep=deep,
    )


class DeepHintTests(unittest.TestCase):
    def _hint(self, channel: str, payload: dict) -> str:
        with patch("workers.async_get_workers_payload", new=AsyncMock(return_value=payload)):
            return asyncio.run(deep_hint(channel, _settings()))

    def test_web_hint_names_no_command_and_no_executor_brand(self):
        hint = self._hint("web", _payload([READY_WORKER]))
        self.assertTrue(hint)
        self.assertNotIn("/deep", hint)
        self.assertNotIn("Codex", hint)
        self.assertIn("执行者", hint)

    def test_bot_channels_keep_their_command_hint(self):
        for channel in ("telegram", "feishu"):
            hint = self._hint(channel, _payload([READY_WORKER]))
            self.assertIn("/deep", hint)

    def test_no_executor_means_no_line_at_all(self):
        for channel in ("web", "telegram", "feishu"):
            self.assertEqual(self._hint(channel, _payload([NOT_INSTALLED_WORKER])), "", channel)
            self.assertEqual(self._hint(channel, _payload([], enabled=False)), "", channel)

    def test_unverified_note_stays_even_without_a_deeper_check(self):
        answer = _unverified_answer(self._hint("web", _payload([NOT_INSTALLED_WORKER])))
        self.assertIn("未联网核实", answer)
        self.assertNotIn("/deep", answer)

    def test_low_confidence_note_keeps_its_warning_without_the_command(self):
        answer = _low_confidence_answer(self._hint("web", _payload([READY_WORKER])))
        self.assertIn("把握不大", answer)
        self.assertNotIn("/deep", answer)
        self.assertNotIn("Codex", answer)


class AskChatWebAnswerTests(unittest.TestCase):
    """End to end: what the web console is told to show carries no slash command."""

    def _ask(self, payload: dict) -> str:
        from handlers import chat as chat_handlers

        async def fake_stream(_config, _messages):
            yield "据我所知大约 6 万美元。\n[[CONFIDENCE: medium]]"

        port = MagicMock()
        port.reply = AsyncMock(return_value="placeholder-1")
        port.edit_progress = AsyncMock(return_value=True)
        msg = InboundMessage(
            channel="web", operator_id="owner", chat_id="default", message_id=None,
            text="今天比特币价格多少",
        )
        with patch("runner.chat_client.config_from_settings", return_value=SimpleNamespace()), \
                patch("runner.chat_client.stream_chat", new=fake_stream), \
                patch.object(chat_handlers, "history", return_value=[]), \
                patch.object(chat_handlers, "remember"), \
                patch("agent_context.enabled", return_value=False), \
                patch("personal_tools.long_term_memory.allowed_for", return_value=False), \
                patch("personal_tools.long_term_memory.owner_for_chat", return_value=None), \
                patch("agents.instructions_for_chat", return_value=("", "")), \
                patch("workers.async_get_workers_payload", new=AsyncMock(return_value=payload)):
            outcome, _checked = asyncio.run(chat_handlers.ask_chat(
                msg, port, _settings(), question="今天比特币价格多少"))
        self.assertEqual(outcome, "answered")
        return str(port.edit_progress.await_args.args[2])

    def test_web_answer_with_an_executor_suggests_a_plain_next_step(self):
        final = self._ask(_payload([READY_WORKER]))
        self.assertIn("未联网核实", final)
        self.assertIn("执行者", final)
        self.assertNotIn("/deep", final)
        self.assertNotIn("Codex", final)

    def test_web_answer_without_an_executor_drops_the_line(self):
        final = self._ask(_payload([NOT_INSTALLED_WORKER]))
        self.assertIn("未联网核实", final)
        self.assertNotIn("/deep", final)
        self.assertNotIn("更深入地查证", final)


class RoutinesOffConsoleContractTests(unittest.TestCase):
    """QA r6 N13: with 定时任务 off the console must not call the 409 endpoints at all.

    The backend keeps its 409 + ``code`` answer (see tests/test_feature_off_codes.py);
    the console gates on the feature flag from /api/system/status instead.
    """

    def test_status_payload_reports_the_flag_the_console_gates_on(self):
        from handlers.job_queue import JobQueue
        from web_control import WebControl

        root = Path(tempfile.mkdtemp(prefix="conveyor-routines-off-"))
        settings = SimpleNamespace(
            codex_memory_root=root, codex_task_root=root, routines_enabled=False,
            telegram_bot_token="", lark_app_id="", lark_app_secret="",
        )
        queue = JobQueue()
        queue.configure(settings, runner=None, recover=False)
        control = WebControl(settings, runner=None, queue=queue)
        with patch.object(WebControl, "nodes", return_value=[]):
            status = control.system_status()
        self.assertFalse(status["features"]["routines"])
        self.assertFalse(status["features"]["webhooks"])

    def test_inbox_panel_skips_the_requests_when_the_flag_is_off(self):
        root = Path(__file__).resolve().parents[1]
        app = (root / "web/src/App.tsx").read_text(encoding="utf-8")
        panel = (root / "web/src/components/InboxPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("routinesEnabled={Boolean(system?.features?.routines)}", app)
        self.assertIn("if (!routinesEnabled)", panel)
        # The owner-facing sentence is unchanged.
        self.assertIn("定时任务功能未开启。", panel)


if __name__ == "__main__":
    unittest.main()
