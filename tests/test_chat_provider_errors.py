"""A failing chat provider ends the turn with a Chinese error; it never starts an agent run."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from channel.types import InboundMessage
from runner.chat_client import ChatError, owner_chat_error_text


def _msg(text: str = "报错测试一下") -> InboundMessage:
    return InboundMessage(channel="web", operator_id="owner", chat_id="default", message_id=None, text=text)


class OwnerChatErrorTextTests(unittest.TestCase):
    def test_http_codes_map_to_chinese_with_the_owner_action(self):
        self.assertIn("API 密钥", owner_chat_error_text("HTTP 401"))
        self.assertIn("API 密钥", owner_chat_error_text("HTTP 403"))
        self.assertIn("接口地址", owner_chat_error_text("HTTP 404"))
        self.assertIn("429", owner_chat_error_text("HTTP 429"))
        self.assertIn("500", owner_chat_error_text("HTTP 500"))
        self.assertIn("503", owner_chat_error_text("HTTP 503"))

    def test_incomplete_stream_points_at_the_interface_type(self):
        text = owner_chat_error_text("Stream ended without finish_reason")
        self.assertIn("Chat Completions", text)
        self.assertNotIn("Stream", text)
        self.assertIn("Chat Completions", owner_chat_error_text("invalid response: no choices"))

    def test_timeout_and_unknown_errors_are_plain_chinese(self):
        self.assertIn("超时", owner_chat_error_text("chat request timed out"))
        fallback = owner_chat_error_text("something odd happened")
        self.assertRegex(fallback, r"[一-鿿]")
        self.assertNotIn("HTTP", fallback)


class ChatTierFailureTests(unittest.TestCase):
    def test_provider_failure_returns_error_outcome_and_keeps_raw_text_for_details(self):
        from handlers.chat import _chat_tier_failed

        port = SimpleNamespace(edit_progress=AsyncMock(return_value=True))
        outcome, checked = asyncio.run(_chat_tier_failed(_msg(), port, "placeholder-1", ChatError("HTTP 500")))
        self.assertEqual(outcome, "error")
        self.assertIn("500", checked.body)
        self.assertEqual(checked.reason, "HTTP 500")
        # Bot ports get the Chinese sentence in the placeholder; nothing says 转交 Codex.
        port.edit_progress.assert_awaited_once()
        self.assertNotIn("Codex", port.edit_progress.await_args.args[2])

    def test_web_port_is_not_edited_here_because_run_web_chat_renders_the_bubble(self):
        from handlers.chat import _chat_tier_failed

        port = MagicMock(handles_transcript_directly=True)
        port.edit_progress = AsyncMock(return_value=True)
        outcome, _ = asyncio.run(_chat_tier_failed(_msg(), port, "web-chat-placeholder", ChatError("HTTP 500")))
        self.assertEqual(outcome, "error")
        port.edit_progress.assert_not_awaited()

    def test_ask_chat_stream_failure_does_not_fall_back_to_an_agent(self):
        from handlers import chat as chat_handlers

        settings = SimpleNamespace(
            skills_enabled=False, chat_tools_enabled=False, web_search_backend="disabled",
            chat_history_turns=5, chat_vision=False, codex_memory_root=Path(tempfile.gettempdir()),
        )

        async def failing_stream(config, messages):
            raise ChatError("HTTP 500")
            yield  # pragma: no cover - makes this an async generator

        port = MagicMock()
        port.reply = AsyncMock(return_value="placeholder-1")
        port.edit_progress = AsyncMock(return_value=True)
        with patch("runner.chat_client.config_from_settings", return_value=SimpleNamespace()), \
                patch("runner.chat_client.stream_chat", new=failing_stream), \
                patch.object(chat_handlers, "history", return_value=[]), \
                patch.object(chat_handlers, "remember"), \
                patch("agent_context.enabled", return_value=False), \
                patch("personal_tools.long_term_memory.allowed_for", return_value=False), \
                patch("personal_tools.long_term_memory.owner_for_chat", return_value=None), \
                patch("agents.instructions_for_chat", return_value=("", "")):
            outcome, checked = asyncio.run(chat_handlers.ask_chat(_msg(), port, settings, question="报错测试一下"))
        self.assertEqual(outcome, "error")
        self.assertIn("500", checked.body)

    def test_chat_or_agent_never_starts_a_codex_job_after_a_provider_error(self):
        from handlers import chat as chat_handlers

        handle_codex_job = AsyncMock()
        web_port = SimpleNamespace(supports_inline_buttons=False, reply=AsyncMock(), reply_with_buttons=AsyncMock())
        with patch.object(chat_handlers, "ask_chat", new=AsyncMock(return_value=("error", None))), \
                patch.object(chat_handlers, "chat_key", return_value="k"), \
                patch.object(chat_handlers, "set_last") as set_last, \
                patch("handlers.jobs.handle_codex_job", new=handle_codex_job):
            asyncio.run(chat_handlers.chat_or_agent(
                _msg(), web_port, SimpleNamespace(), MagicMock(),
                question="报错测试一下", codex_prompt="报错测试一下",
            ))
        handle_codex_job.assert_not_awaited()
        set_last.assert_called_once()
        web_port.reply.assert_not_awaited()  # the web shows its own error bubble
        web_port.reply_with_buttons.assert_not_awaited()

    def test_bot_channels_get_a_button_to_hand_the_request_to_an_agent(self):
        from handlers import chat as chat_handlers

        handle_codex_job = AsyncMock()
        bot_port = SimpleNamespace(supports_inline_buttons=True, reply=AsyncMock(), reply_with_buttons=AsyncMock())
        with patch.object(chat_handlers, "ask_chat", new=AsyncMock(return_value=("error", None))), \
                patch.object(chat_handlers, "chat_key", return_value="k"), \
                patch.object(chat_handlers, "set_last"), \
                patch("handlers.jobs.handle_codex_job", new=handle_codex_job):
            asyncio.run(chat_handlers.chat_or_agent(
                _msg(), bot_port, SimpleNamespace(), MagicMock(),
                question="报错测试一下", codex_prompt="报错测试一下",
            ))
        handle_codex_job.assert_not_awaited()
        bot_port.reply_with_buttons.assert_awaited_once()
        self.assertEqual(bot_port.reply_with_buttons.await_args.args[2][0][0]["callback_data"], "deep")


class WebChatProviderErrorTests(unittest.TestCase):
    def test_run_web_chat_emits_failure_bubble_with_raw_detail_and_no_job(self):
        import web_chat
        from handlers.chat import Checked

        events: list[tuple[str, dict]] = []
        port = MagicMock()
        port.emit.side_effect = lambda name, data: events.append((name, data))
        port._persist_turn = MagicMock()
        checked = Checked(body="模型服务暂时出错（500），可以稍后再试。", confidence="low",
                          removed_links=0, escalate=False, reason="HTTP 500")
        runner = SimpleNamespace(name="codex")
        handle_codex_job = AsyncMock()
        with patch("handlers.chat.ask_chat", new=AsyncMock(return_value=("error", checked))), \
                patch("handlers.jobs.handle_codex_job", new=handle_codex_job), \
                patch("main_router.maybe_route", new=AsyncMock(return_value=False)), \
                patch("handlers.intent.computer_chat_route", return_value=None), \
                patch("handlers.chat.routes_to_agent", return_value=False), \
                patch("handlers.chat.readonly_host_tool", return_value=None), \
                patch("handlers.tools.runner.try_resolve_confirmation", new=AsyncMock(return_value=False)), \
                patch("overnight_chat.route", return_value=None), \
                patch("handlers.intent.project_progress_route", return_value=None), \
                patch("goal_loop.parse_goal_command", return_value=None):
            asyncio.run(web_chat.run_web_chat(_msg(), port, SimpleNamespace(), runner, "报错测试一下"))

        handle_codex_job.assert_not_awaited()
        name, data = next((e for e in events if e[0] == "message"), ("", {}))
        self.assertEqual(name, "message")
        self.assertEqual(data["text"], checked.body)
        self.assertEqual(data["failure"], {"detail": "HTTP 500"})
        self.assertIn(("done", {"outcome": "error"}), events)


if __name__ == "__main__":
    unittest.main()
