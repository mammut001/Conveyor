"""Briefings and reminders must actually reach the owner."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class BriefingDeliveryTest(unittest.TestCase):
    def test_telegram_briefing_uses_a_real_sender(self) -> None:
        from personal_tools.briefing import _send_briefing
        settings = SimpleNamespace(telegram_bot_token="t", telegram_allowed_user_id=42)
        with patch("scripts.telegram_api.send_message") as send:
            _send_briefing("telegram", "-100123:topic:7", "早安", settings)
            _send_briefing("feishu", "oc_1", "早安", settings)
        self.assertEqual(send.call_args_list[0].kwargs["chat_id"], -100123)
        self.assertIn("早安", send.call_args_list[1].args[1])
        self.assertNotIn("chat_id", send.call_args_list[1].kwargs)  # owner DM


class ReminderDeliveryTest(unittest.TestCase):
    def test_web_and_feishu_reminders_reach_the_owner_on_telegram(self) -> None:
        from scripts.scheduler_tick import _deliver_one
        settings = SimpleNamespace(telegram_bot_token="t", telegram_allowed_user_id=42)
        for channel in ("web", "feishu"):
            with self.subTest(channel=channel):
                reminder = SimpleNamespace(id=5, channel=channel, chat_id="x", text="交报告")
                with patch("scripts.telegram_api.send_message") as send:
                    ok, info = _deliver_one(settings, reminder, dry_run=False)
                self.assertTrue(ok, info)
                self.assertIn("交报告", send.call_args.args[1])

    def test_without_telegram_the_failure_is_reported(self) -> None:
        from scripts.scheduler_tick import _deliver_one
        settings = SimpleNamespace(telegram_bot_token="", telegram_allowed_user_id=None)
        ok, info = _deliver_one(settings, SimpleNamespace(id=1, channel="web", chat_id="x", text="t"), dry_run=False)
        self.assertFalse(ok)
        self.assertIn("web", info)


class PastReminderTest(unittest.TestCase):
    def test_reminder_in_the_past_is_refused(self) -> None:
        from personal_tools.reminders import reminders_create
        from tests.test_long_term_memory import _settings
        with tempfile.TemporaryDirectory() as raw:
            settings = _settings(Path(raw))
            result = asyncio.run(reminders_create(settings, "提交报告 2020-01-01T09:00", operator_id="o"))
        self.assertFalse(result.ok)
        self.assertIn("已经过去", result.text)


if __name__ == "__main__":
    unittest.main()
