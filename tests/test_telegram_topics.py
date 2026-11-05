"""Unit tests for Telegram Forum Topics (`telegram_topics.py` & `channel/telegram.py`)."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import agent_channel_routing as routing
import agent_context as ac
from agents import AgentStore
from channel.telegram import TelegramChatOutbound, inbound_from_update, split_chat_thread
from config import Settings
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
        self.assertEqual(msg_fin.chat_type, "p2p")
        self.assertTrue(msg_fin.mentioned_bot)
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
        self.assertEqual(msg_stranger.chat_id, "-100998877")

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
