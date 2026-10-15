"""Deterministic clock-window checks for G4 login quota policy."""
from __future__ import annotations

import unittest

from browser_login_quota_policy import admission_reason


class LoginQuotaPolicyTests(unittest.TestCase):
    def test_three_verified_rejections_lock_not_infrastructure(self):
        now = 1_000_000
        rows = [
            ("failed", "login_open_timeout", False, now),
            ("failed", "login_form_timeout", False, now),
            ("failed", "credentials_rejected", True, now),
            ("failed", "credentials_rejected", True, now),
        ]
        self.assertIsNone(admission_reason(rows, now))
        rows.append(("failed", "credentials_rejected", True, now))
        self.assertEqual(admission_reason(rows, now), "locked")

    def test_busy_legacy_and_success_not_counted(self):
        now = 1_000_000
        rows = ([("failed", "legacy_unverified", False, now)] * 20 +
                [("failed", "busy", False, now)] * 20 +
                [("logged_in", "success", True, now)] * 20 +
                [("needs_human", "authentication_challenge", True, now)] * 20)
        self.assertIsNone(admission_reason(rows, now))

    def test_infrastructure_has_short_separate_cooldown(self):
        now = 1_000_000
        rows = [("failed", "login_open_timeout", False, now)] * 5
        self.assertEqual(admission_reason(rows, now), "infra_rate_limited")
        self.assertIsNone(admission_reason(rows, now + 601))

    def test_inflight_capacity_expires_separately(self):
        now = 1_000_000
        rows = [("started", "in_progress", False, now)] * 3
        self.assertEqual(admission_reason(rows, now), "rate_limited")
        self.assertIsNone(admission_reason(rows, now + 121))

    def test_unverified_submitted_does_not_lock_passwords(self):
        now = 1_000_000
        rows = [("failed", "login_not_confirmed", True, now)] * 3
        self.assertIsNone(admission_reason(rows, now))


if __name__ == "__main__":
    unittest.main()
