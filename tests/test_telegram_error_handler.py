"""Telegram error handler: network blips are a WARNING line, real bugs an ERROR."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error
from telegram import Update


def _load_bot():
    root = tempfile.mkdtemp()
    env = {
        "TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_ALLOWED_USER_ID": "1001",
        "CODEX_WORKSPACE_ROOT": os.path.join(root, "w"),
        "CODEX_TASK_ROOT": os.path.join(root, "t"),
        "CODEX_MEMORY_ROOT": os.path.join(root, "m"),
    }
    with patch.dict(os.environ, env):
        from conveyor.entrypoints import bot
    return bot


class TelegramErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = _load_bot()

    async def _records(self, update, error, level="WARNING"):
        with self.assertLogs("conveyor.telegram", level=level) as cm:
            await self.bot.telegram_error_handler(update, SimpleNamespace(error=error))
        return cm.records

    async def test_polling_network_error_is_one_warning_without_traceback(self):
        for error in (telegram.error.NetworkError("All connection attempts failed"),
                      telegram.error.TimedOut()):
            with self.subTest(error=type(error).__name__):
                records = await self._records(None, error)
                self.assertEqual([r.levelname for r in records], ["WARNING"])
                self.assertIsNone(records[0].exc_info)
                self.assertIn("Telegram network error", records[0].getMessage())

    async def test_network_error_does_not_reply_to_the_user(self):
        update = MagicMock(spec=Update)
        update.effective_message.reply_text = AsyncMock()
        await self._records(update, telegram.error.NetworkError("Server disconnected"))
        update.effective_message.reply_text.assert_not_awaited()

    async def test_real_bug_is_an_error_with_traceback(self):
        records = await self._records(None, ValueError("boom"), level="ERROR")
        self.assertEqual(records[0].getMessage(), "Unhandled exception in handler")
        self.assertIsNotNone(records[0].exc_info)

    async def test_real_bug_with_a_message_replies_once(self):
        update = MagicMock(spec=Update)
        update.effective_message.reply_text = AsyncMock()
        await self._records(update, ValueError("boom"), level="ERROR")
        update.effective_message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
