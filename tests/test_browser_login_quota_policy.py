"""Deterministic clock-window checks for G4 login quota policy."""
from __future__ import annotations

import unittest

from browser_login_quota_policy import admission_reason


class LoginQuotaPolicyTests(unittest.TestCase):
    def test_three_dispatched_failures_lock_not_pre_submit_infra(self):
        now = 1_000_000
        rows = [
            ("failed", "login_open_timeout", False, now),
            ("failed", "login_form_timeout", False, now),
            ("failed", "credentials_rejected", True, now),
            ("failed", "login_not_confirmed", True, now),
        ]
        self.assertIsNone(admission_reason(rows, now))
        rows.append(("failed", "login_form_timeout", True, now))
        self.assertEqual(admission_reason(rows, now), "locked")

    def test_busy_and_success_or_human_do_not_count(self):
        now = 1_000_000
        rows = ([("failed", "busy", False, now)] * 20 +
                [("logged_in", "success", True, now)] * 20 +
                [("needs_human", "authentication_challenge", True, now)] * 20)
        self.assertIsNone(admission_reason(rows, now))

    def test_legacy_generic_failures_keep_existing_lockouts(self):
        now = 1_000_000
        rows = [("failed", "legacy_unverified", False, now)] * 3
        self.assertEqual(admission_reason(rows, now), "locked")

    def test_infrastructure_has_short_separate_cooldown(self):
        now = 1_000_000
        rows = [("failed", "login_open_timeout", False, now)] * 5
        self.assertEqual(admission_reason(rows, now), "infra_rate_limited")
        self.assertIsNone(admission_reason(rows, now + 601))

    def test_inflight_capacity_expires_but_crashed_pre_submit_counts_infra(self):
        now = 1_000_000
        rows = [("started", "in_progress", False, now)] * 3
        self.assertEqual(admission_reason(rows, now), "rate_limited")
        self.assertIsNone(admission_reason(rows, now + 121))
        rows.extend([("started", "in_progress", False, now)] * 2)
        self.assertEqual(admission_reason(rows, now + 121), "infra_rate_limited")

    def test_crashed_submitted_rows_do_not_expire_at_120_seconds(self):
        now = 1_000_000
        rows = [("started", "post_dispatched", True, now - 180)] * 3
        self.assertEqual(admission_reason(rows, now), "locked")
        self.assertEqual(admission_reason(rows, now + 3000), "locked")
        self.assertIsNone(admission_reason(rows, now + 3600))

    def test_dispatched_timeout_5xx_ambiguous_and_takeover_failure_lock(self):
        now = 1_000_000
        for reason in ("login_form_timeout", "login_not_confirmed",
                       "login_unavailable", "takeover_unavailable",
                       "policy_denied", "credentials_rejected"):
            with self.subTest(reason=reason):
                rows = [("failed", reason, True, now)] * 3
                self.assertEqual(admission_reason(rows, now), "locked")
        rows = [("failed", "login_not_confirmed", True, now)] * 2
        rows.append(("logged_in", "success", True, now))
        self.assertIsNone(admission_reason(rows, now))


if __name__ == "__main__":
    unittest.main()
