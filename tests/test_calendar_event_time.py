"""calendar_google._parse_event_time must parse without importing helpers that do not exist."""
from __future__ import annotations

import unittest
from datetime import timedelta

from personal_tools.calendar_google import _parse_event_time


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


if __name__ == "__main__":
    unittest.main()
