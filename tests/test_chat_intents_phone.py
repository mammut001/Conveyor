"""Owner-only phone chat intents: dispatch routing, Telegram action callbacks
(execute-once / replay / wrong-user-or-chat), navigation-only fallback, and the
Feishu numbered-choice flow. Hermetic — fake ports, disposable SQLite/git, no
network, no real executors.
"""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from channel.types import InboundMessage
from conveyor.agents import chat_intents
from conveyor.agents import supervisor_service as svc
from conveyor.agents import supervisor_goals as goals_mod
from handlers import chat_intents_phone
from handlers.dispatch import dispatch
from tests.test_chat_intents import _start_available_executor_patches
from tests.test_supervisor_api import _make_settings


@dataclass
class FakePort:
    replies: list[str] = field(default_factory=list)
    # (text, rows) for every reply_with_buttons call; rows keep callback_data.
    button_msgs: list[tuple[str, list[list[dict]]]] = field(default_factory=list)
    supports_inline_buttons: bool = False
    supports_attachments: bool = False

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "r"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "n"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: Any, text: str) -> bool:
        return True

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons) -> str:
        self.replies.append(text)
        self.button_msgs.append((text, [list(row) for row in buttons]))
        return "b"


def _msg(channel: str, chat_id: str, text: str, *, operator_id: str, chat_type: str = "p2p") -> InboundMessage:
    return InboundMessage(
        channel=channel,
        operator_id=operator_id,
        chat_id=chat_id,
        message_id="m1",
        text=text,
        chat_type=chat_type,
    )


def _cancel_card(settings: Any, session_id: str) -> dict[str, Any]:
    return chat_intents.create_action_card(
        settings, session_id=session_id, title="t", text="选一个",
        buttons=[{"label": "取消", "action": "cancel_action",
                  "server_payload": {"outcome": "已取消测试"}}],
    )


class PhoneChatIntentsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-phone-intents-")
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root / "env")
        self.runner = AsyncMock()
        _start_available_executor_patches(self)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def _tg(self, text: str, *, chat_id: str = "c1", operator_id: str = "1") -> InboundMessage:
        return _msg("telegram", chat_id, text, operator_id=operator_id)

    def _fs(self, text: str, *, chat_id: str = "fc1", operator_id: str = "ou_owner") -> InboundMessage:
        return _msg("feishu", chat_id, text, operator_id=operator_id)

    def _callback_ids(self, port: FakePort) -> list[str]:
        ids: list[str] = []
        for _text, rows in port.button_msgs:
            for row in rows:
                for button in row:
                    cb = str(button.get("callback_data") or "")
                    if cb.startswith("ci:"):
                        ids.append(cb[3:])
        return ids

    def _button_labels(self, port: FakePort) -> list[str]:
        return [b["text"] for _t, rows in port.button_msgs for row in rows for b in row]

    # -- routing gate --------------------------------------------------------

    async def test_dispatch_routes_owner_private_intent(self) -> None:
        port = FakePort(supports_inline_buttons=True)
        await dispatch(self._tg("帮我创建一个 repo 叫 todo-app"), port, self.settings, self.runner)
        # A card was rendered with a single-use inline button, and the repo exists.
        self.assertTrue(port.button_msgs)
        ids = self._callback_ids(port)
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids[0].startswith("act_"))
        self.assertEqual([r["name"] for r in svc.list_repositories(self.settings)], ["todo-app"])

    async def test_maybe_handle_leaves_groups_and_non_owners_alone(self) -> None:
        port = FakePort(supports_inline_buttons=True)
        intent = "帮我创建一个 repo 叫 todo-app"

        group = _msg("telegram", "g1", intent, operator_id="1", chat_type="group")
        self.assertFalse(await chat_intents_phone.maybe_handle(group, port, self.settings, intent))
        self.assertEqual(port.button_msgs, [])
        self.assertEqual(svc.list_repositories(self.settings), [])

        non_owner = self._tg(intent, operator_id="999")
        self.assertFalse(await chat_intents_phone.maybe_handle(non_owner, port, self.settings, intent))
        self.assertEqual(port.button_msgs, [])
        self.assertEqual(svc.list_repositories(self.settings), [])

    async def test_dispatch_refuses_non_owner_before_intents(self) -> None:
        port = FakePort(supports_inline_buttons=True)
        await dispatch(self._tg("帮我创建一个 repo 叫 todo-app", operator_id="999"),
                       port, self.settings, self.runner)
        self.assertEqual(svc.list_repositories(self.settings), [])
        self.assertIn("Unauthorized", "\n".join(port.replies))

    # -- navigation-only buttons become text ---------------------------------

    async def test_navigation_only_button_becomes_text_hint(self) -> None:
        card = chat_intents.create_action_card(
            self.settings, session_id="telegram:c1", title="目标进度",
            text="已经开始了。",
            buttons=[{"label": "查看进度", "action": "open_goal_progress", "client_payload": {}}],
        )
        port = FakePort(supports_inline_buttons=True)
        await chat_intents_phone._render_card(
            self._tg(""), port, self.settings, "telegram:c1", "已经开始了。", card,
        )
        # No pressable button; the web-only navigation becomes a plain hint.
        self.assertEqual(port.button_msgs, [])
        joined = "\n".join(port.replies)
        self.assertIn("网页", joined)
        self.assertNotIn("查看进度", joined)

    async def test_action_button_kept_while_navigation_becomes_hint(self) -> None:
        # The create-project card mixes a nav-only and an actionable button.
        port = FakePort(supports_inline_buttons=True)
        await chat_intents_phone.maybe_handle(
            self._tg("帮我创建一个 repo 叫 todo-app"), port, self.settings,
            "帮我创建一个 repo 叫 todo-app",
        )
        self.assertEqual(self._button_labels(port), ["让它开始干活"])  # 打开项目 is navigation-only
        self.assertIn("网页", "\n".join(port.replies))

    # -- Telegram action callback -------------------------------------------

    async def test_action_runs_once_then_refuses_replay(self) -> None:
        action_id = _cancel_card(self.settings, "telegram:c1")["buttons"][0]["id"]

        port = FakePort()
        await chat_intents_phone.run_action(self.settings, self._tg(""), port, action_id)
        self.assertIn("已取消测试", "\n".join(port.replies))

        replay = FakePort()
        await chat_intents_phone.run_action(self.settings, self._tg(""), replay, action_id)
        self.assertIn("已经处理过", "\n".join(replay.replies))

    async def test_action_refuses_a_different_user_and_a_different_chat(self) -> None:
        # Different user (same chat): refused by the owner gate, action untouched.
        card_user = _cancel_card(self.settings, "telegram:c1")
        port_user = FakePort()
        await chat_intents_phone.run_action(
            self.settings, self._tg("", operator_id="999"), port_user, card_user["buttons"][0]["id"])
        self.assertIn("只有老板本人", "\n".join(port_user.replies))
        self.assertNotIn("已取消测试", "\n".join(port_user.replies))

        # Different chat (owner): refused by the per-chat binding, action untouched.
        card_chat = _cancel_card(self.settings, "telegram:c1")
        port_chat = FakePort()
        await chat_intents_phone.run_action(
            self.settings, self._tg("", chat_id="c2"), port_chat, card_chat["buttons"][0]["id"])
        self.assertIn("不是发给你的操作", "\n".join(port_chat.replies))
        self.assertNotIn("已取消测试", "\n".join(port_chat.replies))

    async def test_telegram_start_goal_button_then_nav_only_follow_up(self) -> None:
        svc.create_managed_project(self.settings, name="todo-app")
        port = FakePort(supports_inline_buttons=True)
        await chat_intents_phone.maybe_handle(
            self._tg("在 todo-app 上迭代 2 小时，把首页做好"), port, self.settings,
            "在 todo-app 上迭代 2 小时，把首页做好",
        )
        start_id = None
        for _text, rows in port.button_msgs:
            for row in rows:
                for button in row:
                    if button["text"] == "开始":
                        start_id = button["callback_data"][3:]
        self.assertIsNotNone(start_id)

        result = FakePort()
        await chat_intents_phone.run_action(self.settings, self._tg(""), result, start_id)
        self.assertTrue(goals_mod.list_goals(self.settings))
        # The "已开始持续改进" follow-up carries only a navigation-only button.
        self.assertEqual(result.button_msgs, [])
        self.assertIn("已经开始了", "\n".join(result.replies))
        self.assertIn("网页", "\n".join(result.replies))

    # -- Feishu numbered fallback -------------------------------------------

    async def test_feishu_numbered_options_resolve_choice_one(self) -> None:
        settings = replace(self.settings, lark_allowed_open_id="ou_owner")
        svc.create_managed_project(settings, name="todo-app")

        port = FakePort()
        await chat_intents_phone.maybe_handle(
            self._fs("在 todo-app 上迭代 2 小时，把首页做好"), port, settings,
            "在 todo-app 上迭代 2 小时，把首页做好",
        )
        # Feishu gets numbered text, not inline buttons.
        self.assertEqual(port.button_msgs, [])
        joined = "\n".join(port.replies)
        self.assertIn("1. 开始", joined)
        self.assertIn("回复数字选择", joined)

        # The owner's next message 「1」 runs the first stored action (开始).
        reply = FakePort()
        await chat_intents_phone.maybe_handle(self._fs("1"), reply, settings, "1")
        self.assertTrue(goals_mod.list_goals(settings))
        self.assertIn("已经开始了", "\n".join(reply.replies))


if __name__ == "__main__":
    unittest.main()
