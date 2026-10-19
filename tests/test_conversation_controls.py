"""Phone control phrases must bypass model latency without swallowing tasks."""
from dataclasses import replace
from pathlib import Path
import importlib
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from channel.types import Attachment, InboundMessage, ReplyContext
from config import Settings
from handlers.commands import parse_command, parse_conversation_control, run_command
from handlers.dispatch import dispatch


class ConversationControlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(
            telegram_bot_token="test", telegram_allowed_user_id=1,
            lark_allowed_open_id="owner", codex_workspace_root=root,
            codex_bin="codex", codex_task_root=root / "tasks", codex_model=None,
            codex_timeout_seconds=30, telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(), codex_memory_root=root / "memory",
            user_timezone="UTC",
        )

    def msg(self, channel, text):
        return InboundMessage(channel=channel, chat_id="phone", message_id="m",
                              operator_id="1" if channel == "telegram" else "owner",
                              text=text, chat_type="p2p")

    def test_only_explicit_whole_phrases_are_controls(self):
        self.assertEqual(parse_conversation_control(" 看看任务进度？ "), ("status", ""))
        for text in ("帮我实现查看任务进度", "解释‘清空对话’", "取消订单",
                     "清空数据库", "怎么用 Python 读取文件", "重新开始", "/help"):
            with self.subTest(text=text):
                self.assertIsNone(parse_conversation_control(text))

    def test_commands_accept_mobile_keyboard_whitespace(self):
        for text in ("/run\tfix this", "/run\nfix this", "/run  fix this"):
            self.assertEqual(parse_command(text), ("run", "fix this"))

    async def test_progress_and_help_bypass_models_in_both_phone_channels(self):
        for channel in ("telegram", "feishu"):
            for text in ("看看任务进度", "查看帮助"):
                with self.subTest(channel=channel, text=text):
                    port, runner = AsyncMock(), Mock()
                    runner.status_text.return_value = "正在运行：测试任务"
                    with patch("main_router.maybe_route", new=AsyncMock()) as model_route:
                        await dispatch(self.msg(channel, text), port, self.settings, runner)
                    model_route.assert_not_awaited()
                    port.reply.assert_awaited_once()
                    if text == "看看任务进度":
                        self.assertIn("正在运行", port.reply.await_args.args[1])
                    else:
                        answer = port.reply.await_args.args[1]
                        self.assertLess(len(answer), 600)
                        self.assertIn("/commands", answer)

    async def test_quoted_or_attached_control_phrase_keeps_its_context(self):
        for channel in ("telegram", "feishu"):
            for extra in ({"reply_to": ReplyContext("解释这些按钮")},
                          {"attachments": (Attachment(kind="image", ref="test"),)}):
                with self.subTest(channel=channel, extra=extra):
                    msg = replace(self.msg(channel, "清空对话"), **extra)
                    with patch.object(importlib.import_module("handlers.dispatch"), "handle_context_job", new=AsyncMock()) as context, \
                            patch.object(importlib.import_module("handlers.dispatch"), "handle_chat_clear", new=AsyncMock()) as clear, \
                            patch("main_router.maybe_route", new=AsyncMock(return_value=False)):
                        await dispatch(msg, AsyncMock(), self.settings, Mock())
                    clear.assert_not_awaited()
                    context.assert_awaited_once()

    async def test_clear_phrase_reaches_existing_clear_handler_in_both_channels(self):
        for channel in ("telegram", "feishu"):
            with self.subTest(channel=channel):
                with patch.object(importlib.import_module("handlers.dispatch"), "handle_chat_clear", new=AsyncMock()) as clear:
                    await dispatch(self.msg(channel, "清空当前对话。"), AsyncMock(), self.settings, Mock())
                clear.assert_awaited_once()

    async def test_unauthorized_control_cannot_read_status(self):
        runner, port = Mock(), AsyncMock()
        msg = replace(self.msg("telegram", "查看任务进度"), operator_id="other")
        await dispatch(msg, port, self.settings, runner)
        runner.status_text.assert_not_called()
        self.assertEqual(port.reply.await_args.args[1], "Unauthorized.")

    async def test_full_command_inventory_remains_available(self):
        port = AsyncMock()
        self.assertTrue(await run_command("commands", self.msg("feishu", "/commands"),
                                          port, Mock(), self.settings, ""))
        self.assertIn("/calendar_search", port.reply.await_args.args[1])
