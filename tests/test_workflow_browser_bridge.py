"""Fixture-only Browser Executor Bridge regression suite: no network or real GUI."""
from __future__ import annotations

import json
import stat
import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from workflow_browser_bridge import FixtureBrowser, execute_fixture_plan
from workflow_browser_contract import InvalidBrowserPlan, parse_plan
from workflow_browser_journal import BrowserAttemptJournal, JournalConflict


def sample_plan():
    return {
        "plan_id": "mock_visitor600_001",
        "fixture": "visitor600_mock",
        "actions": [
            {"kind": "open", "on_page": "idle", "target": "welcome", "expect_page": "welcome"},
            {"kind": "assert_text", "on_page": "welcome", "target": "mock_welcome", "expect_page": "welcome"},
            {"kind": "click", "on_page": "welcome", "target": "next", "expect_page": "details"},
            {"kind": "fill", "on_page": "details", "target": "purpose", "expect_page": "details", "value": "tourism"},
            {"kind": "fill", "on_page": "details", "target": "funding", "expect_page": "details", "value": "self"},
            {"kind": "assert_field", "on_page": "details", "target": "purpose", "expect_page": "details", "value": "tourism"},
            {"kind": "click", "on_page": "details", "target": "next", "expect_page": "review"},
            {"kind": "assert_text", "on_page": "review", "target": "mock_review", "expect_page": "review"},
        ],
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "private"
        self.journal = BrowserAttemptJournal(self.path / "attempts.sqlite3")
        self.browser = FixtureBrowser()
        self.plan = sample_plan()

    def test_full_mock_form_flow_with_observed_readback(self):
        result = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual((result.status, result.completed), ("verified", 8))
        self.assertEqual(self.browser.page, "review")
        self.assertEqual(self.browser.fields, {"purpose": "tourism", "funding": "self"})
        self.assertEqual(len(self.journal.summaries(self.plan["plan_id"])), 8)
        self.assertTrue(all(x["status"] == "verified" for x in self.journal.summaries(self.plan["plan_id"])))

    def test_verified_plan_replay_does_not_click_or_fill_again(self):
        execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        count = self.browser.invocations
        result = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual(result.status, "verified")
        self.assertEqual(self.browser.invocations, count)

    def test_crash_after_click_fails_closed_without_replaying(self):
        self.browser = FixtureBrowser(interrupt_after_operation=3)
        result = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(result.action_index, 2)
        self.assertEqual(self.browser.page, "details")  # side effect may already have happened
        self.assertEqual(self.journal.state(self.plan["plan_id"], 2)["status"], "dispatched")
        count = self.browser.invocations
        again = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual(again.status, "reconciliation_required")
        self.assertEqual(self.browser.invocations, count)

    def test_restart_from_a_verified_prefix(self):
        parsed = parse_plan(self.plan)
        for i in range(3):
            op = parsed.actions[i]
            self.assertEqual(
                self.journal.dispatch(parsed.plan_id, i, parsed.plan_digest, op.kind,
                                      op.on_page, op.expect_page), "dispatched_new",
            )
            self.browser.apply(op)
            self.journal.verify(parsed.plan_id, i, parsed.plan_digest)
        # A different Python runner instance and a re-opened SQLite journal.
        journal2 = BrowserAttemptJournal(self.journal.path)
        result = execute_fixture_plan(self.plan, browser=self.browser, journal=journal2)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.completed, 8)
        self.assertEqual(self.browser.invocations, 8)

    def test_lost_browser_context_never_reopens_old_steps_automatically(self):
        execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        fresh_browser = FixtureBrowser()
        result = execute_fixture_plan(self.plan, browser=fresh_browser, journal=self.journal)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(result.reason, "page_state_drifted")
        self.assertEqual(fresh_browser.invocations, 0)

    def test_changed_plan_idempotency_collision_is_rejected(self):
        execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        changed = sample_plan()
        changed["actions"][3]["value"] = "study"
        result = execute_fixture_plan(changed, browser=self.browser, journal=self.journal)
        self.assertEqual((result.status, result.reason), ("blocked", "plan_changed"))

    def test_failed_assertion_requires_reconciliation(self):
        plan = {"plan_id": "bad_assert_001", "fixture": "visitor600_mock",
                "actions": [
                    {"kind": "open", "on_page": "idle", "target": "welcome", "expect_page": "welcome"},
                    {"kind": "click", "on_page": "welcome", "target": "next", "expect_page": "details"},
                    {"kind": "assert_field", "on_page": "details", "target": "purpose",
                     "expect_page": "details", "value": "tourism"},
                ]}
        result = execute_fixture_plan(plan, browser=self.browser, journal=self.journal)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(result.action_index, 2)
        self.assertEqual(self.journal.state(plan["plan_id"], 2)["status"], "dispatched")

    def test_no_raw_form_values_in_durable_journal(self):
        execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        with self.journal._db() as db:
            rows = db.execute("SELECT * FROM browser_action_attempts").fetchall()
            dumped = json.dumps([dict(r) for r in rows])
        self.assertNotIn("tourism", dumped)
        self.assertNotIn("self", dumped)
        self.assertEqual(stat.S_IMODE(self.journal.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)

    def test_reject_real_urls_and_dangerous_actions(self):
        additions = [
            ("target", "https://immi.homeaffairs.gov.au/"),
            ("target", "http://127.0.0.1:18887/"),
            ("kind", "submit"),
            ("kind", "upload"),
            ("kind", "evaluate_javascript"),
        ]
        for key, value in additions:
            bad = sample_plan()
            bad["actions"][0][key] = value
            with self.subTest(value=value), self.assertRaises(InvalidBrowserPlan):
                parse_plan(bad)
        for field in ("passport", "password", "credit_card", "cvv"):
            bad = sample_plan()
            bad["actions"][3]["target"] = field
            with self.subTest(field=field), self.assertRaises(InvalidBrowserPlan):
                parse_plan(bad)
        bad = sample_plan()
        bad["actions"][3]["value"] = "token=secret-value"
        with self.assertRaises(InvalidBrowserPlan):
            parse_plan(bad)
        bad = sample_plan()
        bad["actions"][3]["selector"] = "button[type=submit]"
        with self.assertRaises(InvalidBrowserPlan):
            parse_plan(bad)

    def test_type_invalid_actions_are_rejected_by_contract(self):
        for key, value in (
            ("kind", []), ("kind", {"x": 1}),
            ("on_page", []), ("on_page", {"page": "idle"}),
            ("expect_page", []), ("expect_page", {"page": "welcome"}),
            ("target", []), ("target", {"selector": "welcome"}),
        ):
            candidate = sample_plan()
            candidate["actions"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(InvalidBrowserPlan):
                parse_plan(candidate)

    def test_completed_review_does_not_hide_drifted_form_field(self):
        first = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual(first.status, "verified")
        count = self.browser.invocations
        self.browser.fields["purpose"] = "tampered"
        replay = execute_fixture_plan(self.plan, browser=self.browser, journal=self.journal)
        self.assertEqual((replay.status, replay.reason), ("reconciliation_required", "field_state_drifted"))
        self.assertEqual(self.browser.invocations, count)

    def test_concurrent_dispatch_reserves_an_action_exactly_once(self):
        parsed = parse_plan(self.plan)
        op = parsed.actions[0]
        def reserve(_i):
            return self.journal.dispatch(parsed.plan_id, 0, parsed.plan_digest,
                                         op.kind, op.on_page, op.expect_page)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(results.count("dispatched_new"), 1)
        self.assertEqual(results.count("dispatched"), 5)
        self.assertEqual(self.journal.state(parsed.plan_id, 0)["status"], "dispatched")

    def test_reject_invalid_or_insecure_journal_paths(self):
        existing = self.path / "public.sqlite3"
        existing.write_bytes(b"")
        existing.chmod(0o644)
        with self.assertRaises(ValueError):
            BrowserAttemptJournal(existing)
        external = self.path / "outside.bin"
        external.write_bytes(b"unchanged")
        symlink = self.path / "insecure.sqlite3"
        symlink.symlink_to(external)
        with self.assertRaises(ValueError):
            BrowserAttemptJournal(symlink)
        self.assertEqual(external.read_bytes(), b"unchanged")

    def test_dispatch_is_exclusive_for_same_action(self):
        parsed = parse_plan(self.plan)
        op = parsed.actions[0]
        self.assertEqual(
            self.journal.dispatch(parsed.plan_id, 0, parsed.plan_digest, op.kind,
                                  op.on_page, op.expect_page), "dispatched_new")
        self.assertEqual(
            self.journal.dispatch(parsed.plan_id, 0, parsed.plan_digest, op.kind,
                                  op.on_page, op.expect_page), "dispatched")
        with self.assertRaises(JournalConflict):
            self.journal.dispatch(parsed.plan_id, 0, "0" * 64, op.kind,
                                  op.on_page, op.expect_page)


if __name__ == "__main__":
    unittest.main()
