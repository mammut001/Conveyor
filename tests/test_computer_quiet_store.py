"""A quiet computer store (nothing armed or running) is not re-parsed on every status poll."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conveyor.desktop import desktop_computer_requests as cr
from tests.test_computer_dispatch import _settings


class QuietStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = _settings(Path(tmp.name), always_direct=False)
        cr._QUIET_SIGNATURE.clear()
        path = cr.computer_requests_path(self.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"arm": {}, "tasks": {"t1": {
            "task_id": "t1", "status": "done",
            "steps": {"s1": {"status": "completed"}},
        }}}), encoding="utf-8")

    def count_loads(self):
        return mock.patch.object(cr, "_load_unlocked", wraps=cr._load_unlocked)

    def test_quiet_store_is_parsed_once(self) -> None:
        with self.count_loads() as load:
            self.assertIsNone(cr.get_active_task(self.settings))
            first = load.call_count
            for _ in range(3):
                self.assertIsNone(cr.get_active_task(self.settings))
                self.assertEqual(cr.arm_remaining_seconds(self.settings), 0)
                self.assertEqual(cr.expire_old_computer(self.settings), 0)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(load.call_count, first)

    def test_a_write_ends_the_shortcut(self) -> None:
        self.assertIsNone(cr.get_active_task(self.settings))
        armed = cr.arm_direct_mode(self.settings, ttl_minutes=5)
        self.assertTrue(armed.get("ok", True))
        self.assertGreater(cr.arm_remaining_seconds(self.settings), 0)
        created = cr.create_computer_task(self.settings, "open the site", direct_mode=True,
                                          max_steps=3, max_seconds=60)
        self.assertTrue(created.get("ok"), created)
        active = cr.get_active_task(self.settings)
        self.assertIsNotNone(active)

    def test_store_with_unfinished_step_is_never_marked_quiet(self) -> None:
        path = cr.computer_requests_path(self.settings)
        path.write_text(json.dumps({"arm": {}, "tasks": {"t1": {
            "task_id": "t1", "status": "done",
            "steps": {"s1": {"status": "pending", "expires_at": "2099-01-01T00:00:00Z"}},
        }}}), encoding="utf-8")
        cr.expire_old_computer(self.settings)
        self.assertEqual(cr._QUIET_SIGNATURE, {})


if __name__ == "__main__":
    unittest.main()
