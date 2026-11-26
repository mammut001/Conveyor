"""Editing a pending approval's argument retires the old token on every channel."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.tools.executors  # noqa: F401  register builtin tools
from conveyor.channels import approval_inbox, approval_relay
from handlers.tools.confirm import clear_all_pending, create_pending, get_pending
from tests.test_approval_relay import MockOutbound, make_test_settings


class EditSupersedesTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        clear_all_pending()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True, channels=("telegram", "feishu"))
        self.tg = approval_relay.FakeNotifier("telegram")
        self.fs = approval_relay.FakeNotifier("feishu")
        approval_relay.set_notifier_factory(lambda ch, _s: self.tg if ch == "telegram" else self.fs)
        approval_relay._rate_limiter.reset()
        self.addCleanup(approval_relay.reset_notifier_factory)
        self.addCleanup(approval_relay._rate_limiter.reset)
        self.addCleanup(clear_all_pending)

        self.old = create_pending("notes.add", "original note", "web-console", "chat-1", "web")
        approval_relay.publish(self.settings, self.old, summary="Add note", danger="write", source="chat")

    def _edit(self):
        return approval_inbox.edit_pending(self.settings, self.old.token, {"text": "edited note"})

    def test_old_token_is_refused_and_new_one_is_sent(self) -> None:
        new = self._edit()
        self.assertNotEqual(new.token, self.old.token)
        self.assertIsNone(get_pending(self.old.token))
        self.assertEqual(get_pending(new.token).arg, "edited note")

        self.assertEqual(approval_relay.get_relay_row(self.settings, self.old.token)["status"], "superseded")
        new_row = approval_relay.get_relay_row(self.settings, new.token)
        self.assertEqual(new_row["status"], "pending")
        self.assertEqual(new_row["arg_preview"], "edited note")
        self.assertEqual(new_row["summary"], "Add note")

        # Both remote channels: the old message says invalid, a new one carries the new arg.
        for notifier in (self.tg, self.fs):
            self.assertEqual([m["token"] for m in notifier.sent], [self.old.token, new.token])
            self.assertEqual(notifier.sent[1]["arg_preview"], "edited note")
            self.assertEqual(len(notifier.updated), 1)
            self.assertIn("已失效", notifier.updated[0]["text"])

        for via in ("telegram", "feishu", "web"):
            self.assertEqual(
                approval_relay.decide(self.settings, self.old.token, approve=True, via=via, decided_by=via),
                "superseded",
            )
        self.assertEqual(approval_relay.get_relay_row(self.settings, new.token)["status"], "pending")

    def test_new_token_approval_executes_the_new_argument(self) -> None:
        new = self._edit()
        outcome = approval_relay.decide(self.settings, new.token, approve=True, via="telegram", decided_by="tg")
        self.assertEqual(outcome, "won")
        consumer = approval_relay.RelayConsumer(self.settings, channel="web")
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as run_tool:
            run_tool.return_value = "saved"
            self.assertEqual(asyncio.run(consumer.poll_once()), 1)
        run_tool.assert_called_once()
        self.assertEqual(run_tool.call_args.args[1:3], ("notes.add", "edited note"))

    def test_telegram_button_for_the_old_token_says_invalid(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test_bot_token", "TELEGRAM_ALLOWED_USER_ID": "12345"}):
            from conveyor.entrypoints import bot
        new = self._edit()
        query = MagicMock()
        query.data = f"relay:approve:{self.old.token}"
        query.answer = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=12345, username="operator"),
            effective_chat=SimpleNamespace(id=12345, type="private"),
            effective_message=MagicMock(),
            callback_query=query,
        )
        with patch("conveyor.entrypoints.bot.settings", self.settings):
            asyncio.run(bot.relay_callback(update, MagicMock()))
        self.assertIn("已失效", query.answer.call_args.args[0])
        self.assertEqual(approval_relay.get_relay_row(self.settings, new.token)["status"], "pending")

    def test_feishu_card_for_the_old_token_says_invalid(self) -> None:
        with patch.dict(os.environ, {
            "LARK_APP_ID": "cli_test", "LARK_APP_SECRET": "sec_test", "LARK_ALLOWED_OPEN_ID": "ou_test_user",
        }):
            from conveyor.entrypoints import feishu_bot
        new = self._edit()
        card = {"event": {
            "operator": {"open_id": "ou_test_user"},
            "action": {"value": {"action": "relay_approve", "token": self.old.token}},
            "context": {"open_chat_id": "oc_test_chat"},
        }}
        port = MockOutbound()
        with patch("conveyor.entrypoints.feishu_bot.settings", self.settings), \
                patch("conveyor.entrypoints.feishu_bot.FeishuOutbound", return_value=port):
            asyncio.run(feishu_bot._handle_card_action(card))
        self.assertTrue(any("已失效" in m for m in port.messages), port.messages)
        self.assertEqual(approval_relay.get_relay_row(self.settings, new.token)["status"], "pending")

    def test_web_decision_on_the_old_token_is_refused(self) -> None:
        from conveyor.web.web_chat import decide_tool_approval

        old_action = self.old
        self._edit()
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as run_tool:
            result = asyncio.run(decide_tool_approval(old_action, True, self.settings))
        self.assertEqual(result["status"], "expired")
        run_tool.assert_not_called()

    def test_edit_after_a_remote_decision_is_refused(self) -> None:
        self.assertEqual(
            approval_relay.decide(self.settings, self.old.token, approve=True, via="telegram", decided_by="tg"),
            "won",
        )
        with self.assertRaises(PermissionError):
            self._edit()
        # The approver's decision applies to what they saw.
        self.assertEqual(get_pending(self.old.token).arg, "original note")
        self.assertEqual(approval_relay.get_relay_row(self.settings, self.old.token)["status"], "approved")


if __name__ == "__main__":
    unittest.main()
