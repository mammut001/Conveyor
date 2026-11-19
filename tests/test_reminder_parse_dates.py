"""Tests for explicit Chinese date parsing in personal_tools.reminder_parse."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from personal_tools.reminder_parse import REMIND_USAGE, parse_reminder_text

TORONTO = ZoneInfo("America/Toronto")
FIXED_NOW = datetime(2026, 11, 19, 10, 0, tzinfo=TORONTO)


class ChineseExplicitDateParseTests(unittest.TestCase):
    def test_explicit_year_month_day_with_period_and_hour(self) -> None:
        got = parse_reminder_text(
            "2027年1月1日上午9点 测试提醒",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertEqual(
            got,
            ("测试提醒", datetime(2027, 1, 1, 14, 0, tzinfo=timezone.utc)),
        )

    def test_month_day_hao_evening_current_year(self) -> None:
        got = parse_reminder_text(
            "12月25号晚上8点 给妈妈打电话",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertEqual(
            got,
            ("给妈妈打电话", datetime(2026, 12, 26, 1, 0, tzinfo=timezone.utc)),
        )

    def test_month_day_without_time_rolls_past_date_to_next_year(self) -> None:
        got = parse_reminder_text(
            "1月3日 交房租",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertEqual(
            got,
            ("交房租", datetime(2027, 1, 3, 14, 0, tzinfo=timezone.utc)),
        )

    def test_chinese_numerals_date_and_half_hour(self) -> None:
        got = parse_reminder_text(
            "十二月三十一日下午三点半 年终总结",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertEqual(
            got,
            ("年终总结", datetime(2026, 12, 31, 20, 30, tzinfo=timezone.utc)),
        )

    def test_explicit_past_year_returns_none(self) -> None:
        got = parse_reminder_text(
            "2025年3月1日上午9点 过去的事",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertIsNone(got)

    def test_invalid_calendar_date_returns_none(self) -> None:
        got = parse_reminder_text(
            "2月30日 无效",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertIsNone(got)

    def test_ambiguous_date_plus_relative_day_returns_none(self) -> None:
        got = parse_reminder_text(
            "2027年1月1日明天9点",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertIsNone(got)

    def test_non_adjacent_date_and_time_strips_both(self) -> None:
        got = parse_reminder_text(
            "提醒我 12月25号 给妈妈打电话 晚上8点",
            tz_name="America/Toronto",
            now=FIXED_NOW,
        )
        self.assertEqual(
            got,
            ("提醒我 给妈妈打电话", datetime(2026, 12, 26, 1, 0, tzinfo=timezone.utc)),
        )

    def test_regression_relative_chinese_phrases(self) -> None:
        tomorrow_8am = datetime(2026, 11, 20, 8, 0, tzinfo=TORONTO).astimezone(timezone.utc)
        today_330pm = datetime(2026, 11, 19, 15, 30, tzinfo=TORONTO).astimezone(timezone.utc)
        in_10m = (FIXED_NOW + timedelta(minutes=10)).astimezone(timezone.utc)

        self.assertEqual(
            parse_reminder_text("明天早上8点 开会", tz_name="America/Toronto", now=FIXED_NOW),
            ("开会", tomorrow_8am),
        )
        self.assertEqual(
            parse_reminder_text("下午3点半 取快递", tz_name="America/Toronto", now=FIXED_NOW),
            ("取快递", today_330pm),
        )
        self.assertEqual(
            parse_reminder_text("10分钟后 喝水", tz_name="America/Toronto", now=FIXED_NOW),
            ("喝水", in_10m),
        )

    def test_usage_mentions_explicit_chinese_date_examples(self) -> None:
        self.assertIn("2027年1月1日上午9点 / 12月25号晚上8点", REMIND_USAGE)


if __name__ == "__main__":
    unittest.main()
