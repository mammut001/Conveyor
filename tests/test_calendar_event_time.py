"""calendar_google._parse_event_time must parse without importing helpers that do not exist."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from personal_tools.calendar_google import (
    _format_event,
    _parse_event_time,
    _parse_time_range,
    _user_tz,
)

TORONTO = ZoneInfo("America/Toronto")
FIXED_NOW = datetime(2026, 11, 19, 15, 0, tzinfo=TORONTO)


class ParseEventTimeTests(unittest.TestCase):
    def test_tomorrow_range(self) -> None:
        start, end, err = _parse_event_time("明天 14:00-15:00")
        self.assertIsNone(err)
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (14, 0, 15, 0))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_tomorrow_single_time_defaults_to_one_hour(self) -> None:
        start, end, err = _parse_event_time("明天 14:00")
        self.assertIsNone(err)
        self.assertEqual((start.hour, start.minute), (14, 0))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_empty_is_an_error_not_an_exception(self) -> None:
        _start, _end, err = _parse_event_time("   ")
        self.assertEqual(err, "时间不能为空")

    def test_tomorrow_single_time_in_toronto_tz(self) -> None:
        start, end, err = _parse_event_time("明天 14:00", tz=TORONTO, now=FIXED_NOW)
        self.assertIsNone(err)
        self.assertEqual(start, datetime(2026, 11, 20, 14, 0, tzinfo=TORONTO))
        self.assertEqual(start.astimezone(timezone.utc), datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_explicit_date_single_time_in_toronto_edt(self) -> None:
        start, end, err = _parse_event_time("2026-06-16 14:00", tz=TORONTO, now=FIXED_NOW)
        self.assertIsNone(err)
        self.assertEqual(start, datetime(2026, 6, 16, 14, 0, tzinfo=TORONTO))
        self.assertEqual(start.astimezone(timezone.utc), datetime(2026, 6, 16, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(end - start, timedelta(hours=1))

    def test_explicit_date_range_in_toronto(self) -> None:
        start, end, err = _parse_event_time("2026-06-16 14:00-15:00", tz=TORONTO, now=FIXED_NOW)
        self.assertIsNone(err)
        self.assertEqual(start, datetime(2026, 6, 16, 14, 0, tzinfo=TORONTO))
        self.assertEqual(end, datetime(2026, 6, 16, 15, 0, tzinfo=TORONTO))

    def test_em_dash_range_uses_today_in_toronto(self) -> None:
        start, end, err = _parse_event_time("14:00—15:30", tz=TORONTO, now=FIXED_NOW)
        self.assertIsNone(err)
        self.assertEqual(start, datetime(2026, 11, 19, 14, 0, tzinfo=TORONTO))
        self.assertEqual(end, datetime(2026, 11, 19, 15, 30, tzinfo=TORONTO))

    def test_user_tz_helper(self) -> None:
        self.assertEqual(_user_tz(SimpleNamespace(user_timezone="Invalid/Zone")), timezone.utc)
        self.assertEqual(_user_tz(SimpleNamespace(user_timezone="")), timezone.utc)
        self.assertEqual(_user_tz(SimpleNamespace(user_timezone="America/Toronto")), TORONTO)

    def test_parse_time_range_preserves_owner_offset(self) -> None:
        time_min, time_max, err = _parse_time_range("2026-06-16 14:00-15:00", tz=TORONTO)
        self.assertIsNone(err)
        self.assertEqual(time_min, "2026-06-16T14:00:00-04:00")
        self.assertEqual(time_max, "2026-06-16T15:00:00-04:00")

    def test_format_event_converts_utc_to_owner_tz(self) -> None:
        ev = {
            "summary": "周会",
            "start": {"dateTime": "2026-11-20T19:00:00Z"},
            "end": {"dateTime": "2026-11-20T20:00:00Z"},
        }
        self.assertEqual(_format_event(ev, tz=TORONTO), "  11-20 14:00–15:00 | 周会")


if __name__ == "__main__":
    unittest.main()
