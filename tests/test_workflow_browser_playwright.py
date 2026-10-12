"""Workflow/browser safety regression tests, collected by unittest or pytest."""
from __future__ import annotations

from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from durable_workflows import WorkflowStore
from workflow_browser_bridge import (
    FixtureBrowser, execute_workflow_browser_plan,
)
from workflow_browser_contract import parse_plan
from workflow_browser_journal import BrowserAttemptJournal, JournalConflict
from workflow_browser_playwright import (
    BrowserPolicyError, FIXTURE_ROOT, LocalPlaywrightBrowser,
    validate_fixture_origin,
)


def plan():
    return {
        "plan_id": "browser_fixture_001",
        "fixture": "visitor600_mock",
        "actions": [
            {"kind": "open", "on_page": "idle", "target": "welcome", "expect_page": "welcome"},
            {"kind": "assert_text", "on_page": "welcome", "target": "mock_welcome",
             "expect_page": "welcome"},
            {"kind": "click", "on_page": "welcome", "target": "next", "expect_page": "details"},
            {"kind": "fill", "on_page": "details", "target": "purpose",
             "expect_page": "details", "value": "tourism"},
            {"kind": "fill", "on_page": "details", "target": "funding",
             "expect_page": "details", "value": "self"},
            {"kind": "assert_field", "on_page": "details", "target": "purpose",
             "expect_page": "details", "value": "tourism"},
            {"kind": "click", "on_page": "details", "target": "next", "expect_page": "review"},
            {"kind": "assert_text", "on_page": "review", "target": "mock_review",
             "expect_page": "review"},
        ],
    }


class BrowserWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = SimpleNamespace(codex_memory_root=root, workflows_enabled=True)
        self.store = WorkflowStore(self.settings)
        self.journal = BrowserAttemptJournal(root / "browser-journal" / "actions.sqlite3")
        item = self.store.create(
            title="Fixture local browser", steps=[{"kind": "browser", "label": "Safe fixture"}],
            request_id="create-browser-001",
        )
        self.id = item["id"]

    def transition(self, action, rid):
        snapshot = self.store.get(self.id)
        return self.store.transition(
            self.id, action=action, expected_revision=snapshot["revision"], request_id=rid,
        )

    def execute(self, browser):
        return execute_workflow_browser_plan(
            plan(), browser=browser, journal=self.journal,
            workflow_store=self.store, workflow_id=self.id,
        )

    def test_requires_running_browser_checkpoint_and_completes_after_journal(self):
        browser = FixtureBrowser()
        self.assertEqual(self.execute(browser).reason, "workflow_not_running")
        self.assertEqual(browser.invocations, 0)
        self.transition("begin", "start-browser001")
        result = self.execute(browser)
        self.assertEqual((result.status, result.completed), ("verified", 8))
        self.assertEqual(self.store.get(self.id)["state"], "completed")
        self.assertEqual(len(self.journal.summaries(plan()["plan_id"])), 8)
        self.assertTrue(all(r["status"] == "verified"
                            for r in self.journal.summaries(plan()["plan_id"])))

    def test_resume_verified_prefix_with_new_store_and_journal(self):
        self.transition("begin", "start-browser001")
        browser = FixtureBrowser()
        parsed = parse_plan(plan())
        for index, op in enumerate(parsed.actions[:3]):
            self.journal.dispatch(parsed.plan_id, index, parsed.plan_digest, op.kind,
                                  op.on_page, op.expect_page)
            browser.apply(op)
            self.journal.verify(parsed.plan_id, index, parsed.plan_digest)
        self.store = WorkflowStore(self.settings)
        self.journal = BrowserAttemptJournal(self.journal.path)
        self.assertEqual(self.execute(browser).status, "verified")
        self.assertEqual(browser.invocations, 8)

    def test_unknown_dispatched_action_never_replayed(self):
        self.transition("begin", "start-browser001")
        browser = FixtureBrowser(interrupt_after_operation=3)
        first = self.execute(browser)
        self.assertEqual((first.status, first.action_index),
                         ("reconciliation_required", 2))
        before = browser.invocations
        self.journal = BrowserAttemptJournal(self.journal.path)
        second = self.execute(browser)
        self.assertEqual(second.reason, "outcome_unknown")
        self.assertEqual(browser.invocations, before)
        self.assertEqual(self.store.get(self.id)["steps"][0]["state"], "running")

    def test_human_takeover_and_reverification_stop_execution(self):
        self.transition("begin", "start-browser001")
        self.transition("request_human", "handoff-browser01")
        browser = FixtureBrowser()
        self.assertEqual(self.execute(browser).reason, "human_takeover")
        self.transition("confirm_human", "confirm-handoff01")
        self.assertEqual(self.execute(browser).reason, "workflow_paused")
        self.assertEqual(browser.invocations, 0)
        self.transition("resume_verified", "resume-reviewed01")
        self.assertEqual(self.execute(browser).status, "verified")

    def test_pause_and_plan_binding_conflict(self):
        self.transition("begin", "start-browser001")
        self.transition("pause", "pause-browser001")
        browser = FixtureBrowser()
        self.assertEqual(self.execute(browser).reason, "workflow_paused")
        self.transition("resume", "resume-browser001")
        parsed = parse_plan(plan())
        self.journal.bind_workflow(parsed.plan_id, "f" * 32, 0, parsed.plan_digest)
        self.assertEqual(self.execute(browser).reason, "plan_changed")
        self.assertEqual(browser.invocations, 0)

    def test_different_plan_id_cannot_rebind_same_workflow_step(self):
        self.transition("begin", "start-browser001")
        parsed = parse_plan(plan())
        self.journal.bind_workflow(parsed.plan_id, self.id, 0, parsed.plan_digest)
        with self.assertRaises(JournalConflict):
            self.journal.bind_workflow("different_plan_001", self.id, 0,
                                       parsed.plan_digest)
        with self.assertRaises(JournalConflict):
            self.journal.bind_workflow(parsed.plan_id, "f" * 32, 0,
                                       parsed.plan_digest)

    def test_takeover_during_action_leaves_uncertain_journal(self):
        self.transition("begin", "start-browser001")
        browser = FixtureBrowser()
        apply = browser.apply

        def interrupted(op):
            apply(op)
            if browser.invocations == 1:
                self.transition("request_human", "handoff-during001")

        browser.apply = interrupted
        result = self.execute(browser)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(result.reason, "human_or_workflow_interruption")
        self.assertEqual(self.journal.state(plan()["plan_id"], 0)["status"], "dispatched")
        self.assertEqual(self.store.get(self.id)["state"], "waiting_for_human")


class BrowserGateTests(unittest.TestCase):
    def test_flag_default_off_and_lazy_dependency(self):
        with patch.dict(os.environ, {"CONVEYOR_REAL_BROWSER": "0"}):
            with self.assertRaisesRegex(BrowserPolicyError, "real_browser_disabled"):
                LocalPlaywrightBrowser(origin="http://127.0.0.1:12345")

    def test_refuse_every_nonfixture_origin(self):
        rejected = [
            "https://127.0.0.1:9090", "http://example.com:9090",
            "http://127.0.0.2:9090", "http://localhost.evil:9090",
            "http://[::1]:9090", "file:///tmp/test.html", "http://localhost:0",
            "http://localhost:9090/x", "http://localhost:9090?x=y",
            "http://evil@localhost:9090", "http://localhost:9090/#secret",
            "http://localhost", "http://127.0.0.1:9090@evil.test",
        ]
        for origin in rejected:
            with self.subTest(origin=origin), self.assertRaises(BrowserPolicyError):
                validate_fixture_origin(origin)
        self.assertEqual(validate_fixture_origin("http://localhost:9090"),
                         "http://localhost:9090")


class LocalFixturePlaywrightTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("playwright") is None:
            self.skipTest("Playwright not installed (optional browser test)")
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(FIXTURE_ROOT)),
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = WorkflowStore(SimpleNamespace(codex_memory_root=root,
                                                    workflows_enabled=True))
        self.journal = BrowserAttemptJournal(root / "journal" / "attempts.sqlite3")
        item = self.store.create(
            title="Local Playwright test",
            steps=[{"kind": "browser", "label": "Navigate local static fixture"}],
            request_id="create-playwright-001",
        )
        self.id = item["id"]
        self.store.transition(self.id, action="begin",
                              expected_revision=0, request_id="begin-browser-001")
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.flags = patch.dict(os.environ, {"CONVEYOR_REAL_BROWSER": "1"})
        self.flags.start()
        self.addCleanup(self.flags.stop)
        self.browser = LocalPlaywrightBrowser(origin=self.origin)
        try:
            self.browser.__enter__()
        except Exception as exc:
            # Missing Chromium binary is optional in unit-only environments.
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Playwright Chromium browser binary not installed")
            raise
        self.addCleanup(self.browser.close)

    def run_plan(self):
        return execute_workflow_browser_plan(
            plan(), browser=self.browser, journal=self.journal,
            workflow_store=self.store, workflow_id=self.id,
        )

    def test_real_dom_action_readback_and_sqlite_checkpoint(self):
        result = self.run_plan()
        self.assertEqual((result.status, result.completed), ("verified", 8))
        self.assertEqual(self.browser.page, "review")
        self.assertTrue(self.browser.field_matches("purpose", "tourism"))
        self.assertEqual(self.store.get(self.id)["state"], "completed")
        self.assertTrue(all(row["status"] == "verified"
                            for row in self.journal.summaries(plan()["plan_id"])))

    def test_nonfixture_path_and_external_network_are_denied(self):
        with self.assertRaises(BrowserPolicyError):
            self.browser._file_for_url(self.origin + "/secret.txt")
        with self.assertRaises(BrowserPolicyError):
            self.browser._file_for_url("https://example.com")
        with self.assertRaises(BrowserPolicyError):
            self.browser._field("password")
        with self.assertRaises(BrowserPolicyError):
            self.browser._field("credit_card")

        # A blocked real request poisons this context; never proceed afterwards.
        with self.assertRaises(Exception):
            self.browser._tab.goto(self.origin + "/private", timeout=5000)
        self.assertTrue(self.browser._violated)

    def test_attested_content_blocks_altered_site_and_redirect(self):
        class Impostor(BaseHTTPRequestHandler):
            redirect = False

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.redirect:
                    self.send_response(302)
                    self.send_header("Location", "https://example.com/escape")
                else:
                    self.send_response(200)
                self.end_headers()
                if not self.redirect:
                    self.wfile.write(b"<html>Not the bundled portal</html>")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        for redirect in (False, True):
            Impostor.redirect = redirect
            with LocalPlaywrightBrowser(origin=origin) as impostor_browser:
                with self.assertRaises(Exception):
                    impostor_browser.apply(parse_plan(plan()).actions[0])
                self.assertTrue(impostor_browser._violated)

    def test_crash_between_dom_readback_and_verify_is_not_replayed(self):
        with patch.object(self.journal, "verify", side_effect=RuntimeError("power loss")):
            first = self.run_plan()
        self.assertEqual(first.status, "reconciliation_required")
        self.assertEqual(self.journal.state(plan()["plan_id"], 0)["status"], "dispatched")
        executed = self.browser.invocations
        again = self.run_plan()
        self.assertEqual(again.reason, "outcome_unknown")
        self.assertEqual(self.browser.invocations, executed)


if __name__ == "__main__":
    unittest.main()
