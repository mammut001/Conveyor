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

class RoutineDstTest(unittest.TestCase):
    def test_fall_back_fires_a_fixed_time_job_once(self) -> None:
        from datetime import datetime, timezone
        from routines import next_fire
        first = next_fire("30 1 * * *", datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc), "America/Toronto")
        second = next_fire("30 1 * * *", first, "America/Toronto")
        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(second, datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc))

    def test_spring_forward_runs_a_job_in_the_missing_hour_after_the_jump(self) -> None:
        from datetime import datetime, timezone
        from routines import next_fire
        got = next_fire("30 2 * * *", datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc), "America/Toronto")
        self.assertEqual(got, datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc))  # 03:00 EDT, not the next day

    def test_hourly_jobs_keep_running_through_the_repeated_hour(self) -> None:
        from datetime import datetime, timezone
        from routines import next_fire
        t, fires = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc), []
        for _ in range(3):
            t = next_fire("0 * * * *", t, "America/Toronto"); fires.append(t.hour)
        self.assertEqual(fires, [5, 6, 7])

class TimezoneConfigTest(unittest.TestCase):
    def test_invalid_user_timezone_falls_back_to_utc_everywhere(self) -> None:
        import os
        from config import load_settings
        with patch.dict(os.environ, {"USER_TIMEZONE": "Mars/Olympus"}):
            self.assertEqual(load_settings(".env.test.example").user_timezone, "UTC")
        with patch.dict(os.environ, {"USER_TIMEZONE": "Asia/Shanghai"}):
            self.assertEqual(load_settings(".env.test.example").user_timezone, "Asia/Shanghai")

class BriefingRemindersTest(unittest.TestCase):
    def test_reminders_due_later_today_are_listed(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from personal_tools.briefing import _build_reminders_section
        from personal_tools.store import PersonalToolsStore
        from tests.test_long_term_memory import _settings
        with tempfile.TemporaryDirectory() as raw:
            settings = _settings(Path(raw))
            tz = ZoneInfo(settings.user_timezone)
            morning = datetime.now(tz).replace(hour=8, minute=0, second=0, microsecond=0)
            store = PersonalToolsStore(settings)
            store.create_reminder("o", "下午开会", morning + timedelta(hours=7))
            store.create_reminder("o", "明天交报告", morning + timedelta(days=1, hours=2))
            text = _build_reminders_section(settings, "o", morning)
        self.assertIn("下午开会", text)
        self.assertNotIn("明天交报告", text)


if __name__ == "__main__":
    unittest.main()
