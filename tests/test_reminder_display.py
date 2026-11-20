"""Reminder replies show times in the owner's timezone, not raw UTC ISO strings."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from personal_tools import reminders
from personal_tools.reminders import _format_due


class FormatDueTests(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

    def test_local_time_weekday_and_no_year_this_year(self) -> None:
        self.assertEqual(_format_due("2026-11-23T15:00:00+00:00", "America/Toronto", now=self.NOW), "11月23日 周一 10:00")

    def test_other_year_is_shown_and_z_suffix_parses(self) -> None:
        self.assertEqual(_format_due("2027-01-01T14:00:00Z", "America/Toronto", now=self.NOW), "2027年1月1日 周五 09:00")

    def test_unknown_timezone_falls_back_to_utc_and_garbage_passes_through(self) -> None:
        self.assertEqual(_format_due("2026-11-23T15:00:00+00:00", "Not/AZone", now=self.NOW), "11月23日 周一 15:00")
        self.assertEqual(_format_due("not a date", "America/Toronto", now=self.NOW), "not a date")


class ReminderRepliesTests(unittest.TestCase):
    def test_create_and_list_show_local_times(self) -> None:
        from tests.test_web_reminders_api import _settings

        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(_settings(Path(tmp)), user_timezone="America/Toronto")
            created = asyncio.run(reminders.reminders_create(settings, "2027年1月1日上午9点 测试", operator_id="42"))
            self.assertTrue(created.ok, created.text)
            self.assertIn("2027年1月1日 周五 09:00", created.text)
            self.assertNotIn("+00:00", created.text)
            listed = asyncio.run(reminders.reminders_list(settings, "", operator_id="42"))
            self.assertIn("2027年1月1日 周五 09:00 · 测试", listed.text)
            self.assertNotIn("T14:00", listed.text)


if __name__ == "__main__":
    unittest.main()
