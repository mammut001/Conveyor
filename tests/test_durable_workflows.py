"""Regression tests for durable workflow checkpoints (no real external side effects)."""
from __future__ import annotations

import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from durable_workflows import WorkflowConflict, WorkflowNotFound, WorkflowStore


STEPS = [
    {"kind": "documents", "label": "Collect required supporting documents"},
    {"kind": "browser", "label": "Prepare the online application draft"},
    {"kind": "human", "label": "Human verifies applicant statements and identity"},
    {"kind": "submission", "label": "Human reviews final lodge/payment checkpoint"},
]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = SimpleNamespace(codex_memory_root=self.root, workflows_enabled=True)
        self.store = WorkflowStore(self.settings)

    def create(self, request_id="create-00000001", steps=None):
        return self.store.create(title="Visitor 600 example workflow",
                                 steps=STEPS if steps is None else steps, request_id=request_id)

    def transition(self, item, action, request_id):
        return self.store.transition(item["id"], action=action,
                                     request_id=request_id, expected_revision=item["revision"])

    def test_disabled_flag_has_no_side_effects(self):
        disabled = SimpleNamespace(codex_memory_root=self.root / "disabled", workflows_enabled=False)
        with self.assertRaises(ValueError):
            WorkflowStore(disabled)
        self.assertFalse((self.root / "disabled").exists())

    def test_store_rejects_unsafe_existing_database(self):
        bad = self.root / "unsafe"
        (bad / "state").mkdir(parents=True)
        db_file = bad / "state" / "workflows.sqlite3"
        db_file.write_bytes(b"")
        db_file.chmod(0o644)
        with self.assertRaises(ValueError):
            WorkflowStore(SimpleNamespace(codex_memory_root=bad, workflows_enabled=True))
        db_file.unlink()
        source = self.root / "external.sqlite3"
        source.write_bytes(b"do not modify")
        db_file.symlink_to(source)
        with self.assertRaises(ValueError):
            WorkflowStore(SimpleNamespace(codex_memory_root=bad, workflows_enabled=True))
        self.assertEqual(source.read_bytes(), b"do not modify")

    def test_create_persists_and_is_readable_across_instances(self):
        item = self.create()
        self.assertEqual(item["revision"], 0)
        self.assertEqual(item["state"], "active")
        self.assertEqual(item["steps"][0]["state"], "ready")
        self.assertEqual(item["steps"][2]["state"], "pending")
        again = WorkflowStore(self.settings)
        self.assertEqual(again.get(item["id"]), item)
        self.assertEqual(again.list()[0], item)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_create_idempotency_rejects_changed_plan(self):
        initial = self.create()
        self.assertEqual(self.create(), initial)
        with self.assertRaises(WorkflowConflict):
            self.create(steps=[{"kind": "work", "label": "Different"}])
        self.assertEqual(len(self.store.list()), 1)

    def test_visa_draft_workflow_cannot_skip_human_or_submission_checkpoints(self):
        item = self.create()
        item = self.transition(item, "begin", "step-a-start")
        self.assertEqual(item["steps"][0]["state"], "running")
        item = self.transition(item, "complete", "step-a-done")
        self.assertEqual(item["steps"][1]["state"], "ready")
        item = self.transition(item, "begin", "step-b-start")
        item = self.transition(item, "complete", "step-b-done")
        self.assertEqual(item["state"], "waiting_for_human")
        self.assertEqual(item["steps"][2]["state"], "waiting_for_human")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "begin", "human-bypass")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "complete", "human-complete-bypass")
        item = self.transition(item, "confirm_human", "human-approval")
        self.assertEqual(item["steps"][3]["state"], "waiting_for_human")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "begin", "submission-auto")
        item = self.transition(item, "confirm_human", "final-user-checkpoint")
        self.assertEqual(item["state"], "completed")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "begin", "after-finish")

    def test_request_human_mid_step_and_resume_with_confirm(self):
        item = self.create(steps=[{"kind": "browser", "label": "Open form"}])
        item = self.transition(item, "begin", "start-browser")
        item = self.transition(item, "request_human", "mfa-handoff")
        self.assertEqual(item["state"], "waiting_for_human")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "complete", "bypass-mfa")
        item = self.transition(item, "confirm_human", "mfa-confirmed")
        self.assertEqual(item["state"], "completed")

    def test_pause_resume_retry_and_terminal_cancel(self):
        item = self.create(steps=[{"kind": "work", "label": "Prepare sample draft"}])
        item = self.transition(item, "pause", "pause-req01")
        self.assertEqual(item["state"], "paused")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "begin", "pause-bypass")
        item = self.transition(item, "resume", "resume-req01")
        item = self.transition(item, "begin", "try1-start")
        item = self.transition(item, "fail", "try1-fail")
        self.assertEqual(item["state"], "failed")
        item = self.transition(item, "retry", "retry01")
        item = self.transition(item, "cancel", "cancel01")
        self.assertEqual(item["state"], "cancelled")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "resume", "resurrect")

    def test_retry_limit(self):
        item = self.create(steps=[{"kind": "work", "label": "Try task"}])
        for i in range(5):
            item = self.transition(item, "begin", f"begin-attempt-{i:02}")
            item = self.transition(item, "fail", f"fail-attempt-{i:02}")
            if i < 4:
                item = self.transition(item, "retry", f"retry-attempt-{i:02}")
        with self.assertRaises(WorkflowConflict):
            self.transition(item, "retry", "retry-sixth-attempt")

    def test_transition_replay_before_revision_check(self):
        item = self.create()
        item = self.transition(item, "begin", "start-req01")
        replay = self.store.transition(item["id"], action="begin", request_id="start-req01",
                                       expected_revision=0)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], 1)
        with self.assertRaises(WorkflowConflict):
            self.store.transition(item["id"], action="fail", request_id="start-req01",
                                  expected_revision=0)
        with self.assertRaises(WorkflowConflict):
            self.store.transition(item["id"], action="complete", request_id="new-req01",
                                  expected_revision=0)

    def test_concurrent_compare_and_swap_prevents_double_progress(self):
        item = self.create()
        def attempt(i):
            return self.store.transition(item["id"], action="begin", expected_revision=0,
                                         request_id=f"concurrent-{i:02}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self._safe_transition(attempt, i), range(4)))
        self.assertEqual(sum(result == "ok" for result in results), 1)
        self.assertEqual(self.store.get(item["id"])["revision"], 1)

    @staticmethod
    def _safe_transition(call, index):
        try:
            call(index)
            return "ok"
        except WorkflowConflict:
            return "conflict"

    def test_invalid_input_does_not_create_workflow(self):
        for steps in (
            [], [{"kind": "invalid", "label": "x"}],
            [{"kind": "work", "label": "good", "secret": "no"}],
            [{"kind": "work", "label": "hi\nthere"}],
        ):
            with self.assertRaises(ValueError):
                self.create(request_id="create-test0001", steps=steps)
        with self.assertRaises(ValueError):
            self.create(request_id="a")
        with self.assertRaises(WorkflowNotFound):
            self.store.get("../secrets")
        self.assertEqual(len(self.store.list()), 0)


if __name__ == "__main__":
    unittest.main()
