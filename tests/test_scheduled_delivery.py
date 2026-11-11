"""Briefings and reminders must actually reach the owner."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
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


class BriefingRetryTest(unittest.TestCase):
    """A failed briefing is retried the same day, but only a few times."""

    def setUp(self) -> None:
        from personal_tools.store import PersonalToolsStore
        from tests.test_long_term_memory import _settings
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.store = PersonalToolsStore(self.settings)
        self.op = "op-1"
        self.store.update_briefing_settings(
            self.op, enabled=True, local_time="09:00", channel="telegram", chat_id="42"
        )
        self.now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
        self.date = "2026-06-15"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _send_fails(self):
        return patch("personal_tools.briefing._send_briefing", side_effect=RuntimeError("telegram down"))

    def _send_works(self):
        return patch("personal_tools.briefing._send_briefing")

    def test_failed_briefing_is_retried_the_same_day(self) -> None:
        from personal_tools.briefing import briefing_check_and_send
        with self._send_fails():
            self.assertEqual(briefing_check_and_send(self.settings, now_utc=self.now), 0)
        self.assertEqual(self.store.briefing_attempts_for_date(self.op, self.date), (False, 1))
        # A later tick the same day still tries, and then succeeds.
        with self._send_works():
            self.assertEqual(briefing_check_and_send(self.settings, now_utc=self.now), 1)
        self.assertEqual(self.store.briefing_attempts_for_date(self.op, self.date), (True, 0))

    def test_sent_day_is_not_retried(self) -> None:
        from personal_tools.briefing import briefing_check_and_send
        self.store.record_briefing_run(self.op, self.date, status="sent")
        with self._send_works() as send:
            self.assertEqual(briefing_check_and_send(self.settings, now_utc=self.now), 0)
        send.assert_not_called()

    def test_after_three_failures_no_more_attempts(self) -> None:
        from personal_tools.briefing import briefing_check_and_send
        for _ in range(3):
            with self._send_fails():
                self.assertEqual(briefing_check_and_send(self.settings, now_utc=self.now), 0)
        self.assertEqual(self.store.briefing_attempts_for_date(self.op, self.date), (False, 3))
        with self._send_works() as send:
            self.assertEqual(briefing_check_and_send(self.settings, now_utc=self.now), 0)
        send.assert_not_called()


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
        from conveyor.automation.routines import next_fire
        first = next_fire("30 1 * * *", datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc), "America/Toronto")
        second = next_fire("30 1 * * *", first, "America/Toronto")
        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertEqual(second, datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc))

    def test_spring_forward_runs_a_job_in_the_missing_hour_after_the_jump(self) -> None:
        from datetime import datetime, timezone
        from conveyor.automation.routines import next_fire
        got = next_fire("30 2 * * *", datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc), "America/Toronto")
        self.assertEqual(got, datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc))  # 03:00 EDT, not the next day

    def test_hourly_jobs_keep_running_through_the_repeated_hour(self) -> None:
        from datetime import datetime, timezone
        from conveyor.automation.routines import next_fire
        t, fires = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc), []
        for _ in range(3):
            t = next_fire("0 * * * *", t, "America/Toronto"); fires.append(t.hour)
        self.assertEqual(fires, [5, 6, 7])

class TimezoneConfigTest(unittest.TestCase):
    def test_invalid_user_timezone_falls_back_to_utc_everywhere(self) -> None:
        import os
        from conveyor.core.config import load_settings
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

class ChineseReminderParseTest(unittest.TestCase):
    def test_common_chinese_time_phrases(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import personal_tools.reminder_parse as rp
        tz = ZoneInfo("Asia/Shanghai")
        fixed = datetime(2026, 11, 6, 14, 0, tzinfo=tz)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed

        cases = {
            "明天早上8点交报告": ("交报告", "11-07 08:00"),
            "下午3点半开会": ("开会", "11-06 15:30"),
            "今晚九点十五分 吃药": ("吃药", "11-06 21:15"),
            "10分钟后 取快递": ("取快递", "11-06 14:10"),
            "2小时后 回电话": ("回电话", "11-06 16:00"),
            "下午1点 午会": ("午会", "11-07 13:00"),  # already past today
            "后天 10:30 体检": ("体检", "11-08 10:30"),
        }
        with patch.object(rp, "datetime", FixedDatetime):
            for text, (body, when) in cases.items():
                with self.subTest(text=text):
                    got = rp.parse_reminder_text(text, tz_name="Asia/Shanghai")
                    self.assertIsNotNone(got)
                    self.assertEqual((got[0], got[1].astimezone(tz).strftime("%m-%d %H:%M")), (body, when))
            self.assertIsNone(rp.parse_reminder_text("普通的一句话", tz_name="Asia/Shanghai"))
            self.assertIsNone(rp.parse_reminder_text("3:30 meeting", tz_name="Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()
