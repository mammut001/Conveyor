"""Persistent private-chat Workers keyboard. The runner is never started."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ConversationHandler

from agents import AgentStore
from handlers.job_queue import JobQueue
from tests.test_agents import _settings
from worker_sessions import WorkerSessionStore

_LABELS = ("👷 我的 Workers", "💬 继续对话", "🔄 切换会话", "📋 查看任务")


def _load_bot(settings, queue):
    """Import bot once with test settings so module load does not read secrets."""
    if "bot" not in sys.modules:
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": "1",
            "CODEX_WORKSPACE_ROOT": str(settings.codex_workspace_root),
        }):
            with patch("config.load_settings", return_value=settings), \
                    patch("runner.CodexRunner", return_value=SimpleNamespace()), \
                    patch("handlers.job_queue.get_job_queue", return_value=queue):
                import bot  # noqa: F401
        # The import-time patch must not stay bound on bot.get_job_queue.
        import handlers.job_queue as job_queue
        sys.modules["bot"].get_job_queue = job_queue.get_job_queue
    return sys.modules["bot"]


class WorkersMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.queue = JobQueue()
        self.queue.configure(self.settings, runner=None, recover=False)
        self.bot = _load_bot(self.settings, self.queue)
        self._prior_settings = self.bot.settings
        self._prior_runner = self.bot.runner
        self._prior_get_job_queue = self.bot.get_job_queue
        self.bot.settings = self.settings
        self.bot.runner = SimpleNamespace()
        queue_patch = patch("handlers.job_queue.get_job_queue", return_value=self.queue)
        queue_patch.start()
        self.addCleanup(queue_patch.stop)
        self.dispatched = patch("bot.dispatch", new_callable=AsyncMock)
        self.dispatch = self.dispatched.start()
        self.addCleanup(self.dispatched.stop)
        self.agents = AgentStore(self.settings)

    def tearDown(self) -> None:
        self.bot.settings = self._prior_settings
        self.bot.runner = self._prior_runner
        self.bot.get_job_queue = self._prior_get_job_queue

    def update(self, text, *, user_id=1, chat_type="private", chat_id=42, mentioned=False):
        message = SimpleNamespace(
            text=text, caption=None, photo=None, document=None, message_id=9,
            message_thread_id=None, is_topic_message=False, entities=(),
            caption_entities=(), reply_to_message=None, quote=None, external_reply=None,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, username="op"),
            effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
            effective_message=message,
            callback_query=None,
        )
        if mentioned:
            message.reply_to_message = SimpleNamespace(
                text="hi", caption=None, message_id=3, forum_topic_created=None,
                from_user=SimpleNamespace(id=99, full_name="Bot"),
            )
            update.get_bot = lambda: SimpleNamespace(username="Bot", id=99)
        return update

    def _texts(self, update) -> str:
        return "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)

    def _markups(self, update):
        return [call.kwargs.get("reply_markup") for call in update.effective_message.reply_text.await_args_list]

    def _profile(self) -> None:
        (self.root / "operator.json").write_text("{}", encoding="utf-8")

    def _keyboard(self, update) -> ReplyKeyboardMarkup:
        found = [item for item in self._markups(update) if isinstance(item, ReplyKeyboardMarkup)]
        self.assertTrue(found)
        keyboard = found[0]
        self.assertTrue(keyboard.is_persistent)
        self.assertTrue(keyboard.resize_keyboard)
        self.assertFalse(keyboard.one_time_keyboard)
        self.assertEqual([button.text for row in keyboard.keyboard for button in row], list(_LABELS))
        return keyboard

    async def test_returning_start_installs_menu_and_lists_workers(self) -> None:
        self._profile()
        self.agents.create({"name": "Alpha", "instructions": "A"})
        update = self.update("/start")
        await self.bot.start_cmd(update, MagicMock())
        body = self._texts(update)
        self.assertIn("你好", body)
        self.assertIn("Alpha", body)
        self.assertIn("我的 Workers", body)
        self._keyboard(update)
        self.dispatch.assert_not_awaited()

    async def test_first_start_keeps_onboarding_and_adds_menu(self) -> None:
        update = self.update("/start")
        await self.bot.start_cmd(update, MagicMock())
        body = self._texts(update)
        self.assertIn("第一次用", body)
        self.assertIn("/onboard", body)
        inline = next(item for item in self._markups(update) if isinstance(item, InlineKeyboardMarkup))
        self.assertEqual(inline.inline_keyboard[0][0].callback_data, "ob:start")
        self._keyboard(update)

    async def test_workers_reinstalls_keyboard_and_keeps_inline_buttons(self) -> None:
        self._profile()
        # This case must run the real /workers dispatcher so the inline list is sent.
        self.dispatched.stop()
        update = self.update("/workers")
        try:
            await self.bot.workers_cmd(update, MagicMock())
        finally:
            self.dispatch = self.dispatched.start()
        self._keyboard(update)
        inline = next(item for item in self._markups(update) if isinstance(item, InlineKeyboardMarkup))
        data = [button.callback_data for row in inline.inline_keyboard for button in row]
        self.assertTrue(any(str(item).startswith("wk:") for item in data))
        self.assertNotIn("应用", [button.text for row in inline.inline_keyboard for button in row])

    async def test_exact_labels_follow_selected_session_without_llm(self) -> None:
        store = WorkerSessionStore(self.settings)
        agent = self.agents.create({"name": "Alpha", "instructions": "A"})
        side = store.create(agent["id"], title="Side A")
        other = store.create(agent["id"], title="Other B")
        store.select("telegram", "42", "1", side["session_id"])
        for label, needle in (
            ("💬 继续对话", "已在这个聊天继续「Side A」"),
            ("📋 查看任务", "Side A 的任务"),
            ("🔄 切换会话", "切换会话"),
        ):
            update = self.update(label)
            await self.bot.text_cmd(update, MagicMock())
            body = self._texts(update)
            self.assertIn(needle, body)
            self.assertNotIn("第一次用", body)
            if label != "🔄 切换会话":
                self.assertNotIn("Other B", body)
            else:
                self.assertIn("Other B", body)
                self.assertIn("Side A", body)
            self.assertEqual(store.selected("telegram", "42", "1")["session_id"], side["session_id"])
        self.assertNotEqual(side["session_id"], other["session_id"])
        self.dispatch.assert_not_awaited()

    async def test_no_selection_shows_list_and_does_not_bind(self) -> None:
        update = self.update("💬 继续对话")
        await self.bot.text_cmd(update, MagicMock())
        body = self._texts(update)
        self.assertIn("先从列表里点一个", body)
        self.assertIn("我的 Workers", body)
        self.assertIsNone(WorkerSessionStore(self.settings).selected("telegram", "42", "1"))
        self.dispatch.assert_not_awaited()

    async def test_archived_selection_fails_closed(self) -> None:
        store = WorkerSessionStore(self.settings)
        agent = self.agents.create({"name": "Alpha", "instructions": "A"})
        side = store.create(agent["id"], title="Side A")
        store.select("telegram", "42", "1", side["session_id"])
        self.assertTrue(store.archive_registered(side["session_id"]))
        update = self.update("📋 查看任务")
        await self.bot.text_cmd(update, MagicMock())
        body = self._texts(update)
        self.assertIn("已选会话不可用", body)
        self.assertNotIn("已在这个聊天继续", body)
        self.assertNotIn("Side A 的任务", body)
        self.dispatch.assert_not_awaited()

    async def test_unauthorized_and_group_get_no_menu(self) -> None:
        private = self.update("/start", user_id=9)
        await self.bot.start_cmd(private, MagicMock())
        self.assertIn("Unauthorized", self._texts(private))
        self.assertFalse(any(isinstance(item, ReplyKeyboardMarkup) for item in self._markups(private)))

        quiet = self.update("👷 我的 Workers", user_id=9, chat_type="group", chat_id=-100)
        await self.bot.text_cmd(quiet, MagicMock())
        quiet.effective_message.reply_text.assert_not_awaited()

        self._profile()
        group = self.update("/start", chat_type="group", chat_id=-100)
        await self.bot.start_cmd(group, MagicMock())
        self.assertNotIn("我的 Workers", self._texts(group))
        self.assertFalse(any(isinstance(item, ReplyKeyboardMarkup) for item in self._markups(group)))

        mentioned = self.update("👷 我的 Workers", chat_type="group", chat_id=-100, mentioned=True)
        await self.bot.text_cmd(mentioned, MagicMock())
        self.dispatch.assert_awaited()
        self.assertFalse(any(isinstance(item, ReplyKeyboardMarkup) for item in self._markups(mentioned)))

    async def test_onboarding_does_not_save_menu_label_as_name(self) -> None:
        update = self.update("👷 我的 Workers")
        context = SimpleNamespace(user_data={"onboarding_draft": {}})
        state = await self.bot.onboard_name(update, context)
        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("operator_name", context.user_data["onboarding_draft"])
        self.assertFalse((self.root / "operator.json").exists())
        self.assertIn("/onboard", self._texts(update))
        again = self.update("💬 继续对话")
        state = await self.bot.onboard_menu_interrupt(again, context)
        self.assertEqual(state, ConversationHandler.END)
        self.assertNotIn("operator_name", context.user_data["onboarding_draft"])
        self.assertIn("/onboard", self._texts(again))

    async def test_onboarding_complete_and_skip_restore_keyboard(self) -> None:
        query = AsyncMock()
        query.data = "ob:style:terse"
        finished = self.update("")
        finished.callback_query = query
        context = SimpleNamespace(user_data={"onboarding_draft": {
            "operator_name": "Ada",
            "operator_language": "zh-CN",
        }})
        state = await self.bot.onboard_style_button(finished, context)
        self.assertEqual(state, ConversationHandler.END)
        self._keyboard(finished)

        skipped = self.update("/skip")
        state = await self.bot.onboard_cancel(skipped, SimpleNamespace(user_data={}))
        self.assertEqual(state, ConversationHandler.END)
        self._keyboard(skipped)
        self.assertIn("/onboard", self._texts(skipped))
