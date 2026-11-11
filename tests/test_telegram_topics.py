"""Unit tests for Telegram Forum Topics (`telegram_topics.py` & `channel/telegram.py`)."""
from __future__ import annotations

from dataclasses import replace
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_coordination import agent_channel_routing as routing
import agent_context as ac
from agents import AgentStore
from channel.telegram import TelegramChatOutbound, inbound_from_update, split_chat_thread
from config import Settings
from personal_tools.long_term_memory import allowed_for
from telegram.error import BadRequest
import telegram_topics


def _settings(root: Path) -> Settings:
    return replace(
        Settings(
            telegram_bot_token="test",
            telegram_allowed_user_id=1001,
            codex_workspace_root=root / "workspace",
            codex_bin="codex",
            codex_task_root=root / "tasks",
            codex_model=None,
            codex_timeout_seconds=30,
            codex_retry_429_delays_seconds=(),
            telegram_progress_seconds=1,
            codex_memory_root=root,
            user_timezone="UTC",
            agents_enabled=True,
        ),
        chat_mode="auto",
        chat_api_key="test",
        chat_model="mock",
    )


class TelegramTopicsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = _settings(Path(tmp.name))
        flags = patch.dict(
            os.environ,
            {
                "CONVEYOR_NAMED_AGENTS": "1",
                "CONVEYOR_AGENT_CONTEXT": "1",
                "CONVEYOR_AGENT_MAILBOX": "1",
                "CONVEYOR_AGENT_DELEGATION": "1",
                "CONVEYOR_MAIN_ROUTER": "1",
                "CONVEYOR_AGENT_CHANNEL_ROUTING": "1",
            },
        )
        flags.start()
        self.addCleanup(flags.stop)
        self.store = AgentStore(self.settings)
        self.finance = self.store.create(
            {"name": "Finance", "persona": "整理每月账单与预算", "tools_allowlist": []}
        )
        self.coder = self.store.create(
            {"name": "Coder", "persona": "负责代码维护与测试", "tools_allowlist": []}
        )

    def _fake_update(
        self,
        text: str,
        *,
        user_id: int = 1001,
        chat_id: int = -100998877,
        chat_type: str = "supergroup",
        is_forum: bool = True,
        is_topic_message: bool = True,
        thread_id: int | None = 42,
    ):
        user = SimpleNamespace(id=user_id, full_name="Owner", username="owner")
        chat = SimpleNamespace(id=chat_id, type=chat_type, is_forum=is_forum)
        msg = SimpleNamespace(
            message_id=777,
            text=text,
            caption=None,
            from_user=user,
            chat=chat,
            is_topic_message=is_topic_message,
            message_thread_id=thread_id,
            reply_to_message=None,
            quote=None,
            external_reply=None,
            photo=None,
            document=None,
            entities=(),
            caption_entities=(),
        )
        bot = SimpleNamespace(username="ConveyorBot", id=999999)
        return SimpleNamespace(
            effective_user=user,
            effective_chat=chat,
            effective_message=msg,
            get_bot=lambda: bot,
        )

    async def test_confirm_button_in_bound_topic_uses_the_topic_session(self) -> None:
        """Pending actions in a bound topic live under <chat>:topic:<tid>; the
        confirm button must resolve to that same scope, not the bare chat."""
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_ALLOWED_USER_ID": "1001"}):
            import bot
        telegram_topics.bind_topic(self.settings, chat_id=-100998877, thread_id=42,
                                   operator="1001", agent_id=self.finance["id"])
        update = self._fake_update("", thread_id=42)
        update.callback_query = SimpleNamespace(data="tool:confirm:tok123", answer=AsyncMock(),
                                                message=update.effective_message)
        seen = {}

        async def fake_execute(inbound, port, settings, token):
            seen["chat_id"], seen["token"] = inbound.chat_id, token

        with patch("bot.settings", self.settings), patch("bot._guard", AsyncMock(return_value=True)), \
                patch("bot.execute_confirmed", fake_execute), patch("bot.make_outbound", lambda u: None):
            await bot.tool_callback(update, SimpleNamespace())
        self.assertEqual(seen, {"chat_id": "-100998877:topic:42", "token": "tok123"})

    async def test_topics_bind_ignores_a_reply_thread_outside_a_forum_topic(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_ALLOWED_USER_ID": "1001"}):
            import bot
        update = self._fake_update("/topics bind Finance", is_forum=False, is_topic_message=False, thread_id=55)
        replies = []

        async def fake_reply(_update, text, **_k):
            replies.append(text)

        with patch("bot.settings", self.settings), patch("bot._guard", AsyncMock(return_value=True)), \
                patch("bot._reply", fake_reply), patch("telegram_topics.bind_topic") as bind:
            await bot.topics_cmd(update, SimpleNamespace(args=["bind", "Finance"]))
        bind.assert_not_called()
        self.assertTrue(replies and "话题" in replies[-1])

    async def test_sync_forum_topics_creates_and_pins_per_agent_topics(self) -> None:
        next_tid = 100

        async def create_forum_topic(*, chat_id, name, icon_color):
            nonlocal next_tid
            next_tid += 1
            return SimpleNamespace(message_thread_id=next_tid, name=name)

        sent_messages: list[dict] = []
        pinned_messages: list[tuple] = []

        async def send_message(**kwargs):
            mid = 500 + len(sent_messages)
            sent_messages.append({"message_id": mid, **kwargs})
            return SimpleNamespace(message_id=mid)

        async def pin_chat_message(*, chat_id, message_id, disable_notification=True):
            pinned_messages.append((chat_id, message_id, disable_notification))
            return True

        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(side_effect=create_forum_topic),
            send_message=AsyncMock(side_effect=send_message),
            pin_chat_message=AsyncMock(side_effect=pin_chat_message),
        )

        result = await telegram_topics.sync_forum_topics(
            bot, -100998877, "1001", self.settings
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 3)
        self.assertEqual(len(pinned_messages), 3)
        self.assertIn("竖排话题栏已同步完成", result["text"])

        # Re-running sync reuses existing topics without creating duplicates.
        second = await telegram_topics.sync_forum_topics(
            bot, -100998877, "1001", self.settings
        )
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(len(second["existing"]), 3)

        # Verify each created topic routes to its bound Agent and has isolated context.
        finance_topic = next(item for item in result["created"] if item["agent_name"] == "Finance")
        coder_topic = next(item for item in result["created"] if item["agent_name"] == "Coder")

        upd_fin = self._fake_update("本月信用卡账单是多少", thread_id=finance_topic["thread_id"])
        msg_fin = telegram_topics.contextualize_inbound(self.settings, inbound_from_update(upd_fin))
        self.assertEqual(msg_fin.chat_id, f"-100998877:topic:{finance_topic['thread_id']}")
        # Routes like a DM, but the real chat type is preserved (supergroup forum).
        self.assertTrue(msg_fin.owner_topic)
        self.assertEqual(msg_fin.chat_type, "group")
        self.assertTrue(msg_fin.mentioned_bot)
        self.assertTrue(routing.authorized(self.settings, msg_fin))
        self.assertEqual(
            routing.bound(self.settings, "telegram", msg_fin.chat_id),
            self.finance["id"],
        )
        self.assertEqual(
            ac.resolve(self.settings, "telegram", msg_fin.chat_id, msg_fin.operator_id).agent_id,
            self.finance["id"],
        )

        upd_cod = self._fake_update("跑一下单元测试", thread_id=coder_topic["thread_id"])
        msg_cod = telegram_topics.contextualize_inbound(self.settings, inbound_from_update(upd_cod))
        self.assertEqual(
            ac.resolve(self.settings, "telegram", msg_cod.chat_id, msg_cod.operator_id).agent_id,
            self.coder["id"],
        )
        self.assertNotEqual(
            ac.history_key(self.settings, "telegram", msg_fin.chat_id, msg_fin.operator_id),
            ac.history_key(self.settings, "telegram", msg_cod.chat_id, msg_cod.operator_id),
        )

    async def test_non_owner_or_unbound_group_topic_not_elevated(self) -> None:
        # Unbound group topic stays group and mentioned_bot=False.
        upd_unbound = self._fake_update("普通群聊消息", thread_id=999)
        msg_unbound = telegram_topics.contextualize_inbound(
            self.settings, inbound_from_update(upd_unbound)
        )
        self.assertEqual(msg_unbound.chat_type, "group")
        self.assertFalse(msg_unbound.mentioned_bot)
        self.assertFalse(msg_unbound.owner_topic)
        self.assertEqual(msg_unbound.chat_id, "-100998877")

        # Bound topic, but non-owner user: never elevated.
        telegram_topics.bind_topic(
            self.settings,
            chat_id=-100998877,
            thread_id=88,
            operator="1001",
            agent_id="Finance",
        )
        upd_stranger = self._fake_update("尝试冒充", user_id=2002, thread_id=88)
        msg_stranger = telegram_topics.contextualize_inbound(
            self.settings, inbound_from_update(upd_stranger)
        )
        self.assertEqual(msg_stranger.chat_type, "group")
        self.assertFalse(msg_stranger.mentioned_bot)
        self.assertFalse(msg_stranger.owner_topic)
        self.assertEqual(msg_stranger.chat_id, "-100998877")

    async def test_bound_group_topic_routes_like_dm_but_stays_a_group(self) -> None:
        """A bound topic in a supergroup forum: DM routing, group privacy."""
        telegram_topics.bind_topic(
            self.settings,
            chat_id=-100998877,
            thread_id=42,
            operator="1001",
            agent_id="Finance",
        )
        upd = self._fake_update("整理一下这个月的预算", thread_id=42)
        msg = telegram_topics.contextualize_inbound(
            self.settings, inbound_from_update(upd)
        )
        # chat_type is preserved: other members read this topic.
        self.assertEqual(msg.chat_type, "group")
        self.assertEqual(msg.chat_id, "-100998877:topic:42")
        self.assertTrue(msg.owner_topic)
        self.assertTrue(msg.mentioned_bot)
        # Still routes to the bound Agent exactly like a DM.
        self.assertTrue(routing.authorized(self.settings, msg))
        self.assertEqual(
            routing.bound(self.settings, "telegram", msg.chat_id),
            self.finance["id"],
        )
        self.assertEqual(
            ac.resolve(self.settings, "telegram", msg.chat_id, msg.operator_id).agent_id,
            self.finance["id"],
        )
        # Private-chat-only privacy guards still see a group.
        self.assertFalse(allowed_for(self.settings, msg))
        self.assertTrue(allowed_for(
            replace(self.settings, long_term_memory_groups=True), msg
        ))
        # A non-owner in the same topic is not elevated, so the group guards
        # below cannot be bypassed by anybody else.
        stranger = telegram_topics.contextualize_inbound(
            self.settings,
            inbound_from_update(self._fake_update("在吗", user_id=2002, thread_id=42)),
        )
        self.assertEqual(stranger.chat_type, "group")
        self.assertFalse(stranger.owner_topic)
        self.assertFalse(routing.authorized(self.settings, stranger))

    async def test_private_chat_topic_keeps_p2p_behaviour(self) -> None:
        """Bot Threaded Mode private chat: scoped like before, chat_type stays p2p."""
        upd = self._fake_update(
            "你好", chat_id=1001, chat_type="private", thread_id=7
        )
        msg = telegram_topics.contextualize_inbound(
            self.settings, inbound_from_update(upd)
        )
        self.assertEqual(msg.chat_id, "1001:topic:7")
        self.assertEqual(msg.chat_type, "p2p")
        self.assertTrue(msg.owner_topic)
        self.assertTrue(msg.mentioned_bot)
        self.assertTrue(routing.authorized(self.settings, msg))
        self.assertTrue(allowed_for(self.settings, msg))

    async def test_outbound_passes_message_thread_id(self) -> None:
        self.assertEqual(split_chat_thread("-100998877:topic:102"), (-100998877, 102))
        self.assertEqual(split_chat_thread("1001"), (1001, None))
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=321))
        )
        port = TelegramChatOutbound(bot, "-100998877:topic:102")
        mid = await port.send_new(
            inbound_from_update(self._fake_update("hi", thread_id=102)),
            "Topic scoped reply",
        )
        self.assertEqual(mid, "321")
        bot.send_message.assert_awaited_once_with(
            chat_id=-100998877,
            text="Topic scoped reply",
            disable_web_page_preview=True,
            message_thread_id=102,
        )

    def _active_agents(self) -> list[dict]:
        return [a for a in self.store.list() if not a.get("archived")]

    def _bind_all(self, agents: list[dict], *, base_tid: int = 200) -> list[int]:
        tids: list[int] = []
        for i, agent in enumerate(agents):
            tid = base_tid + i
            telegram_topics.bind_topic(
                self.settings,
                chat_id=-100998877,
                thread_id=tid,
                operator="1001",
                agent_id=agent["id"],
            )
            tids.append(tid)
        return tids

    def _make_create(self, *, start: int = 900, delay: float = 0.0) -> AsyncMock:
        state = {"tid": start}

        async def create_forum_topic(*, chat_id, name, icon_color):
            if delay:
                await asyncio.sleep(delay)
            state["tid"] += 1
            return SimpleNamespace(message_thread_id=state["tid"], name=name)

        return AsyncMock(side_effect=create_forum_topic)

    def _make_send(self) -> AsyncMock:
        sent: list[dict] = []

        async def send_message(**kwargs):
            mid = 500 + len(sent)
            sent.append({"message_id": mid, **kwargs})
            return SimpleNamespace(message_id=mid)

        return AsyncMock(side_effect=send_message)

    def _make_pin(self) -> AsyncMock:
        async def pin_chat_message(*, chat_id, message_id, disable_notification=True):
            return True

        return AsyncMock(side_effect=pin_chat_message)

    async def test_dead_topic_binding_is_recreated(self) -> None:
        """A topic the owner deleted in Telegram is unbound and recreated."""
        agents = self._active_agents()
        old_tids = self._bind_all(agents)

        # Sanity: all agents already look bound before the sync runs.
        self.assertEqual(
            {row["agent_id"] for row in telegram_topics.list_topic_bindings(self.settings, -100998877)},
            {a["id"] for a in agents},
        )

        bot = SimpleNamespace(
            create_forum_topic=self._make_create(),
            send_message=self._make_send(),
            pin_chat_message=self._make_pin(),
            send_chat_action=AsyncMock(
                side_effect=BadRequest("Bad Request: message thread not found")
            ),
        )

        result = await telegram_topics.sync_forum_topics(bot, -100998877, "1001", self.settings)
        self.assertTrue(result["ok"])
        # Every dead binding is dropped and a fresh topic created for it.
        self.assertEqual(len(result["created"]), len(agents))
        self.assertEqual(len(result["existing"]), 0)
        self.assertEqual(bot.create_forum_topic.await_count, len(agents))

        # The old (dead) thread ids are gone and the new ones are stored.
        rows = telegram_topics.list_topic_bindings(self.settings, -100998877)
        new_tids = {row["thread_id"] for row in rows}
        for old in old_tids:
            self.assertNotIn(old, new_tids)
        self.assertEqual(
            {row["agent_id"] for row in rows},
            {a["id"] for a in agents},
        )
        for item in result["created"]:
            self.assertIn(item["thread_id"], new_tids)

    async def test_transient_probe_error_keeps_binding(self) -> None:
        """A non-dead probe error keeps the binding and never creates a topic."""
        agents = self._active_agents()
        old_tids = self._bind_all(agents)

        bot = SimpleNamespace(
            create_forum_topic=self._make_create(),
            send_message=self._make_send(),
            pin_chat_message=self._make_pin(),
            send_chat_action=AsyncMock(side_effect=Exception("network")),
        )

        result = await telegram_topics.sync_forum_topics(bot, -100998877, "1001", self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["existing"]), len(agents))
        bot.create_forum_topic.assert_not_awaited()

        # Bindings are untouched.
        rows = telegram_topics.list_topic_bindings(self.settings, -100998877)
        self.assertEqual({row["thread_id"] for row in rows}, set(old_tids))

    async def test_healthy_binding_probed_once_and_reused(self) -> None:
        """A live topic is probed exactly once and reused without recreating."""
        agents = self._active_agents()
        self._bind_all(agents)

        bot = SimpleNamespace(
            create_forum_topic=self._make_create(),
            send_message=self._make_send(),
            pin_chat_message=self._make_pin(),
            send_chat_action=AsyncMock(return_value=True),
        )

        result = await telegram_topics.sync_forum_topics(bot, -100998877, "1001", self.settings)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 0)
        self.assertEqual(len(result["existing"]), len(agents))
        bot.create_forum_topic.assert_not_awaited()
        # One liveness probe per existing binding.
        self.assertEqual(bot.send_chat_action.await_count, len(agents))

    async def test_concurrent_sync_creates_one_topic_per_agent(self) -> None:
        """Concurrent `/topics` runs are serialized: one topic per agent."""
        agents = self._active_agents()

        bot = SimpleNamespace(
            create_forum_topic=self._make_create(start=1000, delay=0.05),
            send_message=self._make_send(),
            pin_chat_message=self._make_pin(),
            send_chat_action=AsyncMock(return_value=True),
        )

        r1, r2 = await asyncio.gather(
            telegram_topics.sync_forum_topics(bot, -100998877, "1001", self.settings),
            telegram_topics.sync_forum_topics(bot, -100998877, "1001", self.settings),
        )
        # Exactly one topic created per agent across both concurrent runs.
        self.assertEqual(bot.create_forum_topic.await_count, len(agents))
        self.assertEqual(len(r1["created"]) + len(r2["created"]), len(agents))
        # Every agent ends up with exactly one binding.
        rows = telegram_topics.list_topic_bindings(self.settings, -100998877)
        self.assertEqual(len(rows), len(agents))
        self.assertEqual(
            {row["agent_id"] for row in rows},
            {a["id"] for a in agents},
        )

    async def test_non_forum_chat_returns_setup_guide(self) -> None:
        bot = SimpleNamespace(
            create_forum_topic=AsyncMock(side_effect=RuntimeError("Bad Request: the chat is not a forum"))
        )
        res = await telegram_topics.sync_forum_topics(bot, 1001, "1001", self.settings)
        self.assertFalse(res["ok"])
        self.assertIn("竖排话题栏", res["text"])
        self.assertIn("Finance", res["text"])


if __name__ == "__main__":
    unittest.main()
