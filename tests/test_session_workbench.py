"""Session-native refinement and run history, including channel isolation."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from handlers.job_queue import JobQueue
from refinement_store import RefinementStore
from transcript_store import get_transcript_store, session_identity
from web_control import WebControl


class SessionWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = SimpleNamespace(
            codex_memory_root=root,
            codex_task_root=root / "tasks",
            conveyor_max_pending_jobs=10,
            conveyor_event_retention_per_job=100,
        )
        self.queue = JobQueue()
        self.runner = SimpleNamespace(current_job=None)
        self.queue.configure(self.settings, self.runner, recover=False)
        self.control = WebControl(self.settings, self.runner, self.queue)
        self.store = RefinementStore(self.settings)
        self.web_session = session_identity("web", "same", "alice")
        self.telegram_session = session_identity("telegram", "same", "alice")
        for channel, key in (("web", self.web_session), ("telegram", self.telegram_session)):
            get_transcript_store(self.settings).append_turn(
                key, "fix the UI", "done", channel=channel,
                operator_id="alice", source_chat_id="same",
            )

    def insert_job(self, job_id, channel, *, state="completed", turn=None):
        now = datetime.now(timezone.utc).isoformat()
        key = session_identity(channel, "same", "alice")
        metadata = {"refinement_turn": turn} if turn else {}
        conn = self.queue._get_conn()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO queued_jobs
                       (id, operator_id, channel, chat_id, mode, prompt, state,
                        created_at, updated_at, position, metadata_json, session_id)
                       VALUES (?, 'alice', ?, 'same', 'run', ?, ?, ?, ?, 0, ?, ?)""",
                    (job_id, channel, "same prompt " + job_id, state, now, now,
                     json.dumps(metadata), key),
                )
        finally:
            conn.close()

    def make_active(self):
        return self.store.bind_new(
            session_id=self.web_session, channel="web", operator_id="alice",
            source_chat_id="same", worktree_path=Path(self.temp.name) / "private" / "wt",
            runtime_job_id="runtime-w1", queue_job_id="web-1",
        )

    def test_active_chain_is_authoritative_and_scoped(self):
        self.insert_job("web-1", "web", turn=1)
        self.insert_job("tg-1", "telegram")
        active = self.make_active()
        sessions = {item["id"]: item for item in self.control.list_sessions()}
        self.assertEqual(sessions[self.web_session]["active_refinement"]["chain_id"], active["id"])
        self.assertEqual(sessions[self.web_session]["active_refinement"]["turn_count"], 1)
        self.assertEqual(sessions[self.web_session]["latest_job"]["id"], "web-1")
        self.assertEqual(sessions[self.telegram_session]["latest_job"]["id"], "tg-1")
        self.assertIsNone(sessions[self.telegram_session]["active_refinement"])
        # The server never sends paths (or the owner runtime's log contents).
        self.assertNotIn(str(Path(self.temp.name)), json.dumps(sessions))
        self.assertNotIn("worktree_path", json.dumps(sessions))

    def test_historical_runs_and_state_survive_switch_and_close(self):
        self.insert_job("web-1", "web", turn=1)
        self.insert_job("web-2", "web", turn=2)
        self.insert_job("tg-1", "telegram")
        active = self.make_active()
        self.store.continue_active(
            session_id=self.web_session, chain_id=active["id"],
            expected_worktree_path=active["worktree_path"],
            runtime_job_id="runtime-w2", queue_job_id="web-2",
        )
        detail = self.control.get_session(self.web_session)
        self.assertEqual({run["id"] for run in detail["runs"]}, {"web-1", "web-2"})
        self.assertTrue(all(run["id"].startswith("web-") for run in detail["runs"]))
        self.assertEqual(detail["active_refinement"]["turn_count"], 2)
        self.assertEqual(detail["active_refinement"]["latest_queue_job_id"], "web-2")
        self.assertNotIn("worktree_path", json.dumps(detail["active_refinement"]))
        self.store.close_active_for_worktree(active["worktree_path"], state="applied", reason="approved")
        detail = self.control.get_session(self.web_session)
        self.assertIsNone(detail["active_refinement"])
        self.assertEqual(len(detail["runs"]), 2)

    def test_legacy_ambiguous_chat_id_refused(self):
        self.insert_job("web-1", "web")
        self.insert_job("tg-1", "telegram")
        # Two channels share a raw chat ID. An un-namespaced legacy fallback
        # must never combine those jobs in one response.
        self.assertIsNone(self.control.get_session("same"))

    def test_many_colliding_channel_jobs_cannot_hide_own_runs(self):
        self.insert_job("web-1", "web")
        for index in range(220):
            self.insert_job(f"tg-{index:03d}", "telegram")
        detail = self.control.get_session(self.web_session)
        self.assertEqual([run["id"] for run in detail["runs"]], ["web-1"])
        self.assertEqual([job["id"] for job in detail["jobs"]], ["web-1"])

    def test_batch_summaries_do_not_leak_other_sessions(self):
        active = self.make_active()
        self.assertEqual(self.store.active_summaries([]), {})
        grouped = self.store.active_summaries([
            self.web_session, self.telegram_session, self.web_session, "not-found",
        ])
        self.assertEqual(list(grouped), [self.web_session])
        self.assertEqual(grouped[self.web_session]["chain_id"], active["id"])
        self.assertNotIn("worktree_path", grouped[self.web_session])


if __name__ == "__main__":
    unittest.main()
