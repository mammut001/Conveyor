"""Dead forum topic fallback for TelegramChatOutbound.

When the forum topic behind `<chat_id>:topic:<thread_id>` is closed or
deleted Telegram rejects every send with BadRequest ("Message thread
not found", "TOPIC_CLOSED", "TOPIC_DELETED", ...). TelegramChatOutbound
must retry once in the base chat instead of silently dropping the
message (goal-loop notices, approval relays, agent results).

Uses a fake async bot; no real Telegram network.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import telegram.error
from channel.telegram import (
    TelegramChatOutbound,
    _is_dead_topic_error,
    inbound_from_update,
    split_chat_thread,
)
from telegram import InlineKeyboardMarkup

PREFIX = "（原话题已关闭或删除，改发到群聊）\n"
TOPIC_CHAT_ID = "-100998877:topic:102"
BASE_CHAT_ID = -100998877


def _inbound() -> SimpleNamespace:
    """Minimal InboundMessage-shaped object (send paths never read it)."""
    return inbound_from_update(
        SimpleNamespace(
            effective_user=SimpleNamespace(id=1001, username="alice"),
            effective_chat=SimpleNamespace(id=-100998877, type="supergroup"),
            effective_message=SimpleNamespace(
                message_id=777, text="hi", caption=None,
                reply_to_message=None, is_topic_message=True,
                message_thread_id=102,
            ),
            get_bot=lambda: SimpleNamespace(username="", id=None),
        )
    )


class FakeBot:
    """Async bot whose send_message fails for topic-scoped sends only."""

    def __init__(
        self,
        *,
        thread_error: str = "Message thread not found",
        base_error: BaseException | None = None,
    ) -> None:
        self.thread_error = thread_error
        self.base_error = base_error
        self.calls: list[dict] = []
        self.next_id = 555

    async def send_message(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        if kwargs.get("message_thread_id") is not None:
            raise telegram.error.BadRequest(self.thread_error)
        if self.base_error is not None:
            raise self.base_error
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)


class AlwaysDeadTopicBot(FakeBot):
    """Even the base-chat retry fails (topic error for every send)."""

    async def send_message(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        raise telegram.error.BadRequest(self.thread_error)


class DeadTopicErrorDetectionTests(unittest.TestCase):
    def test_detects_all_phrasings(self) -> None:
        for msg in (
            "Bad Request: message thread not found",
            "Bad Request: TOPIC_CLOSED",
            "Bad Request: topic_deleted",
            "Bad Request: topic closed",
            "Bad Request: Topic Deleted",
            "Bad Request: thread not found",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(_is_dead_topic_error(telegram.error.BadRequest(msg)))

    def test_ignores_other_errors(self) -> None:
        for exc in (
            telegram.error.BadRequest("Chat not found"),
            telegram.error.BadRequest("message to edit not found"),
            RuntimeError("boom"),
        ):
            with self.subTest(exc=exc):
                self.assertFalse(_is_dead_topic_error(exc))


class TelegramChatOutboundDeadTopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_new_falls_back_to_base_chat(self) -> None:
        bot = FakeBot()
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        inbound = _inbound()

        with self.assertLogs("conveyor.channel.telegram", level="WARNING") as logs:
            mid = await port.send_new(inbound, "Topic scoped reply")

        self.assertEqual(mid, "556")  # fallback message id, as str
        self.assertEqual(len(bot.calls), 2)
        first, second = bot.calls
        self.assertEqual(first["chat_id"], BASE_CHAT_ID)
        self.assertEqual(first["message_thread_id"], 102)
        # Fallback: base chat, no thread, prefixed text.
        self.assertEqual(second["chat_id"], BASE_CHAT_ID)
        self.assertNotIn("message_thread_id", second)
        self.assertTrue(second["text"].startswith(PREFIX))
        self.assertIn("Topic scoped reply", second["text"])
        self.assertTrue(any("closed/deleted" in line for line in logs.output))

    async def test_fallback_uses_split_chat_thread_base(self) -> None:
        bot = FakeBot()
        port = TelegramChatOutbound(bot, "-100112233:topic:77")
        await port.send_new(_inbound(), "hello")
        target, _thread = split_chat_thread("-100112233:topic:77")
        self.assertEqual(bot.calls[1]["chat_id"], target)

    async def test_non_topic_bad_request_is_not_retried(self) -> None:
        bot = FakeBot(thread_error="Chat not found")
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        self.assertIsNone(await port.send_new(_inbound(), "hello"))
        self.assertEqual(len(bot.calls), 1)

    async def test_plain_chat_id_dead_topic_error_is_not_retried(self) -> None:
        bot = FakeBot(thread_error="Message thread not found")
        bot.send_message = AsyncMock(
            side_effect=telegram.error.BadRequest("Message thread not found")
        )
        port = TelegramChatOutbound(bot, "-100998877")
        self.assertIsNone(await port.send_new(_inbound(), "hello"))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertNotIn("message_thread_id", bot.send_message.await_args.kwargs)

    async def test_fallback_failure_returns_none(self) -> None:
        bot = AlwaysDeadTopicBot()
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        self.assertIsNone(await port.send_new(_inbound(), "hello"))
        self.assertEqual(len(bot.calls), 2)

    async def test_reply_with_buttons_falls_back_and_keeps_reply_markup(self) -> None:
        bot = FakeBot()
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        buttons = [[{"text": "Approve", "callback_data": "tool:confirm:abc"}]]

        mid = await port.reply_with_buttons(_inbound(), "Approve this?", buttons)

        self.assertEqual(mid, "556")
        self.assertEqual(len(bot.calls), 2)
        fallback = bot.calls[1]
        self.assertEqual(fallback["chat_id"], BASE_CHAT_ID)
        self.assertNotIn("message_thread_id", fallback)
        self.assertTrue(fallback["text"].startswith(PREFIX))
        markup = fallback["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(
            [b.callback_data for b in markup.inline_keyboard[0]],
            ["tool:confirm:abc"],
        )

    async def test_reply_with_buttons_non_topic_error_not_retried(self) -> None:
        bot = FakeBot(thread_error="Chat not found")
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        buttons = [[{"text": "Approve", "callback_data": "tool:confirm:abc"}]]
        self.assertIsNone(
            await port.reply_with_buttons(_inbound(), "Approve this?", buttons)
        )
        self.assertEqual(len(bot.calls), 1)

    async def test_fallback_prefix_cannot_overflow_the_limit(self) -> None:
        from redaction import MAX_TELEGRAM_MESSAGE, truncate

        bot = FakeBot()
        port = TelegramChatOutbound(bot, TOPIC_CHAT_ID)
        long_text = "x" * (MAX_TELEGRAM_MESSAGE * 2)
        await port.send_new(_inbound(), long_text)
        fallback_text = bot.calls[1]["text"]
        self.assertEqual(fallback_text, truncate(PREFIX + long_text))
        self.assertLessEqual(len(fallback_text), MAX_TELEGRAM_MESSAGE)

class EditFloodControlTests(unittest.TestCase):
    def _update(self, exc):
        from types import SimpleNamespace
        bot = SimpleNamespace()

        async def edit_message_text(**_kw):
            raise exc

        bot.edit_message_text = edit_message_text
        return SimpleNamespace(
            effective_message=SimpleNamespace(), effective_chat=SimpleNamespace(id=1),
            get_bot=lambda: bot)

    def test_flood_control_skips_the_frame_instead_of_breaking_progress(self) -> None:
        import asyncio
        from telegram.error import BadRequest, RetryAfter
        from channel.telegram import edit_text
        self.assertTrue(asyncio.run(edit_text(self._update(RetryAfter(5)), "10", "x")))
        self.assertFalse(asyncio.run(edit_text(self._update(BadRequest("Message to edit not found")), "10", "x")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
