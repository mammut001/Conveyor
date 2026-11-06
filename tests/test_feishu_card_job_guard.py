"""Feishu card buttons must act on the job their card shows."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(os.environ, {"LARK_APP_ID": "cli_test", "LARK_APP_SECRET": "sec_test",
                             "LARK_ALLOWED_OPEN_ID": "ou_test_user"}):
    import feishu_bot


def _runner(last_id: str, running_id: str | None = None):
    return SimpleNamespace(
        _last_job_id=lambda: last_id,
        current_job=SimpleNamespace(id=running_id) if running_id else None,
    )


class FeishuCardJobGuardTest(unittest.TestCase):
    def _check(self, runner, action, card_job_id):
        inbound = SimpleNamespace(channel="feishu", chat_id="oc_1")
        with patch("job_lanes.runner_for_chat", lambda r, *a, **k: r):
            return feishu_bot._stale_card_job(runner, inbound, action, card_job_id)

    def test_old_card_apply_is_refused(self) -> None:
        text = self._check(_runner("job-new"), "apply", "job-old")
        self.assertIn("job-old", text)
        self.assertIn("job-new", text)

    def test_current_card_apply_and_discard_pass(self) -> None:
        for action in ("apply", "discard", "diff"):
            with self.subTest(action=action):
                self.assertEqual(self._check(_runner("job-1"), action, "job-1"), "")

    def test_cancel_targets_the_running_job_only(self) -> None:
        self.assertEqual(self._check(_runner("job-2", running_id="job-2"), "cancel", "job-2"), "")
        self.assertIn("已不在运行", self._check(_runner("job-3", running_id="job-3"), "cancel", "job-2"))
        self.assertIn("已不在运行", self._check(_runner("job-2"), "cancel", "job-2"))

    def test_cards_without_job_id_and_other_actions_are_untouched(self) -> None:
        self.assertEqual(self._check(_runner("x"), "apply", None), "")
        self.assertEqual(self._check(_runner("x"), "status", "job-1"), "")


if __name__ == "__main__":
    unittest.main()
