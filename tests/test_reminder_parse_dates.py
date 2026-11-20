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


FRIDAY_NOW = datetime(2026, 11, 20, 10, 0, tzinfo=TORONTO)


class ChineseWeekdayParseTests(unittest.TestCase):
    def test_next_monday_morning(self) -> None:
        got = parse_reminder_text(
            "下周一上午10点 开会",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("开会", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
        )

    def test_unprefixed_wednesday_evening(self) -> None:
        got = parse_reminder_text(
            "星期三晚上8点 打电话",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("打电话", datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)),
        )

    def test_unprefixed_sunday_noon(self) -> None:
        got = parse_reminder_text(
            "礼拜天中午12点 吃饭",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("吃饭", datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)),
        )

    def test_unprefixed_today_future_time_stays_today(self) -> None:
        got = parse_reminder_text(
            "周五下午3点 交报告",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("交报告", datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)),
        )

    def test_unprefixed_today_past_time_rolls_to_next_week(self) -> None:
        got = parse_reminder_text(
            "周五上午9点 晨会",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("晨会", datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)),
        )

    def test_this_week_saturday_morning(self) -> None:
        got = parse_reminder_text(
            "这周六上午9点 健身",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("健身", datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)),
        )

    def test_ben_prefix_saturday_morning(self) -> None:
        got = parse_reminder_text(
            "本周六上午9点 健身",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("健身", datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)),
        )

    def test_this_week_past_day_returns_none(self) -> None:
        got = parse_reminder_text(
            "这周三 复盘",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertIsNone(got)

    def test_next_next_tuesday_defaults_to_9am(self) -> None:
        got = parse_reminder_text(
            "下下周二 体检",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("体检", datetime(2026, 12, 1, 14, 0, tzinfo=timezone.utc)),
        )

    def test_next_friday_same_weekday_is_next_week(self) -> None:
        got = parse_reminder_text(
            "下周五下午3点 周报",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("周报", datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)),
        )

    def test_ambiguous_weekday_plus_relative_day_returns_none(self) -> None:
        got = parse_reminder_text(
            "下周一明天9点",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertIsNone(got)

    def test_numeric_sunday_seven(self) -> None:
        got = parse_reminder_text(
            "周7上午10点 x",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("x", datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)),
        )

    def test_non_adjacent_weekday_and_time_strips_both(self) -> None:
        got = parse_reminder_text(
            "提醒我 周五 交报告 下午3点",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("提醒我 交报告", datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)),
        )

    def test_explicit_date_wins_over_weekday(self) -> None:
        got = parse_reminder_text(
            "11月23日 下周一 上午10点 开会",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("下周一 开会", datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)),
        )

    def test_regression_tomorrow_morning(self) -> None:
        got = parse_reminder_text(
            "明天早上8点 开会",
            tz_name="America/Toronto",
            now=FRIDAY_NOW,
        )
        self.assertEqual(
            got,
            ("开会", datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)),
        )

    def test_usage_mentions_weekday_examples(self) -> None:
        self.assertIn("下周一上午10点 / 周五下午3点", REMIND_USAGE)


class ChineseRelativeAndBareTimeParseTests(unittest.TestCase):
    def test_combined_and_half_hour_durations(self) -> None:
        cases = [
            ("1小时30分钟后 收衣服", "收衣服", datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)),
            ("半小时后 喝水", "喝水", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)),
            ("半个小时后 喝水", "喝水", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)),
            ("一个半小时后 开会", "开会", datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)),
            ("两个小时后 开会", "开会", datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)),
            ("90分钟后 出发", "出发", datetime(2026, 11, 20, 16, 30, tzinfo=timezone.utc)),
        ]
        for text, body, due_utc in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_reminder_text(text, tz_name="America/Toronto", now=FRIDAY_NOW),
                    (body, due_utc),
                )

    def test_days_later_with_and_without_time(self) -> None:
        cases = [
            ("两天后上午10点 体检", "体检", datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc)),
            ("3天后 交房租", "交房租", datetime(2026, 11, 23, 14, 0, tzinfo=timezone.utc)),
            ("三天后晚上8点 聚餐", "聚餐", datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)),
        ]
        for text, body, due_utc in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_reminder_text(text, tz_name="America/Toronto", now=FRIDAY_NOW),
                    (body, due_utc),
                )

    def test_ambiguous_days_later_plus_day_word_returns_none(self) -> None:
        self.assertIsNone(
            parse_reminder_text("两天后明天10点 体检", tz_name="America/Toronto", now=FRIDAY_NOW)
        )

    def test_day_after_tomorrow_plus_one_da_houtian(self) -> None:
        self.assertEqual(
            parse_reminder_text("大后天早上7点 跑步", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("跑步", datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)),
        )

    def test_next_month_day(self) -> None:
        self.assertEqual(
            parse_reminder_text("下个月3号 还信用卡", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("还信用卡", datetime(2026, 12, 3, 14, 0, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("下月3号晚上8点 还信用卡", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("还信用卡", datetime(2026, 12, 4, 1, 0, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("下个月31号 x", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("x", datetime(2026, 12, 31, 14, 0, tzinfo=timezone.utc)),
        )

    def test_next_month_invalid_day_and_year_rollover(self) -> None:
        jan_now = datetime(2026, 1, 15, 10, 0, tzinfo=TORONTO)
        self.assertIsNone(
            parse_reminder_text("下个月30号 无效", tz_name="America/Toronto", now=jan_now)
        )
        dec_now = datetime(2026, 12, 15, 10, 0, tzinfo=TORONTO)
        self.assertEqual(
            parse_reminder_text("下个月3号 还信用卡", tz_name="America/Toronto", now=dec_now),
            ("还信用卡", datetime(2027, 1, 3, 14, 0, tzinfo=timezone.utc)),
        )

    def test_bare_hhmm_and_day_word_hhmm(self) -> None:
        self.assertEqual(
            parse_reminder_text("10:30 开会", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("开会", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("10：30 开会", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("开会", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("09:00 晨会", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("晨会", datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("明天10:30 开会", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("开会", datetime(2026, 11, 21, 15, 30, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("大后天 10:30 体检", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("体检", datetime(2026, 11, 23, 15, 30, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("站会 tomorrow 09:00", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("站会", datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            parse_reminder_text("2026-11-20T10:30:00 提交报告", tz_name="America/Toronto", now=FRIDAY_NOW),
            ("提交报告", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc)),
        )

    def test_usage_mentions_new_relative_examples(self) -> None:
        self.assertIn("半小时后", REMIND_USAGE)
        self.assertIn("3天后", REMIND_USAGE)
        self.assertIn("下个月3号", REMIND_USAGE)


if __name__ == "__main__":
    unittest.main()
