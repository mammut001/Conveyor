from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from channel.types import InboundMessage
from handlers.job_queue import JobQueue
from refinement_store import (
    APPLIED,
    DISCARDED,
    RefinementStore,
    is_refinement_feedback,
    should_route_refinement,
    stable_session_id,
)
from runner.operators.jobs import apply_job, discard_job
from runner.types import Job, JobMode, JobState
from runner.worktree import (
    _copy_validated_untracked_files,
    _create_worktree,
    _git,
    _job_worktree_path,
    _remove_worktree,
    _validate_reuse_worktree,
)


class Harness:
    _git = _git
    _job_worktree_path = _job_worktree_path
    _create_worktree = _create_worktree
    _validate_reuse_worktree = _validate_reuse_worktree
    _remove_worktree = _remove_worktree
    _copy_validated_untracked_files = _copy_validated_untracked_files
    apply_job = apply_job
    discard_job = discard_job

    def __init__(self, settings) -> None:
        self.settings = settings
        self.current_job = None
        self.last_job = None


class FakePort:
    def __init__(self) -> None:
        self.is_codex = False

    async def reply(self, _msg, _text):
        return None


class MultiTurnRefinementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        self.settings = SimpleNamespace(
            codex_workspace_root=self.repo.resolve(),
            codex_task_root=(root / "tasks").resolve(),
            codex_memory_root=(root / "memory").resolve(),
            conveyor_max_worktrees_bytes=1024 * 1024 * 1024,
            conveyor_apply_allow_high_risk=False,
            conveyor_apply_max_untracked_bytes=1024 * 1024,
            conveyor_max_pending_jobs=10,
            conveyor_event_retention_per_job=100,
            user_timezone="UTC",
        )
        (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
        self.runner = Harness(self.settings)
        self.store = RefinementStore(self.settings)
        self.session = stable_session_id("web", "chat-a", "operator")

    def tearDown(self) -> None:
        # Worktrees can make TemporaryDirectory cleanup noisy on Windows; the
        # project CI is POSIX, but prune best-effort keeps this fixture tidy.
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, check=False, capture_output=True)
        self.temp.cleanup()

    def _job(self, runtime_id: str, *, session_id: str | None = None) -> Job:
        job = Job(runtime_id, JobMode.FIX, "refine", "danger-full-access")
        if session_id:
            job.refinement_session_id = session_id
            job.refinement_channel = "web"
            job.refinement_operator_id = "operator"
            job.refinement_source_chat_id = session_id.rsplit(":", 1)[-1]
            job.refinement_turn = 1
        return job

    def _first(self, runtime_id: str = "runtime-a", *, session_id: str | None = None):
        job = self._job(runtime_id, session_id=session_id or self.session)
        path = asyncio.run(self.runner._create_worktree(job))
        active = self.store.active(session_id or self.session)
        self.assertIsNotNone(active)
        return job, path, active

    def _continue(self, runtime_id: str, active: dict):
        job = self._job(runtime_id, session_id=active["session_id"])
        job.reuse_worktree_path = Path(active["worktree_path"])
        job.refinement_chain_id = active["id"]
        job.refinement_owner_job_id = active["owner_runtime_job_id"]
        job.refinement_root_queue_job_id = active["root_queue_job_id"]
        job.refinement_parent_queue_job_id = active["latest_queue_job_id"]
        job.reused_worktree = True
        path = asyncio.run(self.runner._create_worktree(job))
        return job, path, self.store.active(active["session_id"])

    def test_first_turn_creates_and_binds_new_worktree(self):
        job, path, active = self._first()
        self.assertTrue(path.exists())
        self.assertEqual(Path(active["worktree_path"]), path)
        self.assertEqual(active["owner_runtime_job_id"], job.id)
        self.assertEqual(active["turn_count"], 1)

    def test_second_turn_reuses_exact_same_worktree_and_sees_first_changes(self):
        _, first_path, active = self._first()
        (first_path / "README.md").write_text("base\nturn one\n", encoding="utf-8")
        _, second_path, active2 = self._continue("runtime-b", active)
        self.assertEqual(second_path, first_path)
        self.assertEqual((second_path / "README.md").read_text(encoding="utf-8"), "base\nturn one\n")
        self.assertEqual(active2["turn_count"], 2)

    def test_third_turn_accumulates_in_same_worktree(self):
        _, first_path, active = self._first()
        (first_path / "README.md").write_text("base\none\n", encoding="utf-8")
        _, second_path, active = self._continue("runtime-b", active)
        (second_path / "README.md").write_text("base\none\ntwo\n", encoding="utf-8")
        _, third_path, active = self._continue("runtime-c", active)
        (third_path / "extra.txt").write_text("three\n", encoding="utf-8")
        self.assertEqual(first_path, second_path)
        self.assertEqual(second_path, third_path)
        self.assertEqual(active["turn_count"], 3)
        status = subprocess.run(
            ["git", "status", "--short"], cwd=third_path, check=True, capture_output=True, text=True
        ).stdout
        self.assertIn("README.md", status)
        self.assertIn("extra.txt", status)

    def test_different_sessions_never_reuse_worktree(self):
        _, path_a, _ = self._first()
        session_b = stable_session_id("web", "chat-b", "operator")
        _, path_b, _ = self._first("runtime-b", session_id=session_b)
        self.assertNotEqual(path_a, path_b)
        self.assertNotEqual(self.store.active(self.session)["id"], self.store.active(session_b)["id"])

    def test_restart_recovers_active_binding_from_sqlite(self):
        _, path, active = self._first()
        restarted = RefinementStore(self.settings)
        recovered = restarted.active(self.session)
        self.assertEqual(recovered["id"], active["id"])
        self.assertEqual(Path(recovered["worktree_path"]), path)

    def test_queued_followup_persists_refinement_intent_before_worktree_exists(self):
        queue = JobQueue()
        queue.configure(self.settings, self.runner, recover=False)
        msg = InboundMessage(
            channel="web", operator_id="operator", chat_id="queue-chat",
            message_id="m1", text="first", chat_type="p2p",
        )
        port = FakePort()

        async def scenario():
            ok1, _, first = await queue.enqueue("fix", "first", msg, port, self.runner)
            self.assertTrue(ok1)
            claimed = await queue.dequeue(require_idle=True)
            self.assertEqual(claimed.id, first.id)
            msg2 = InboundMessage(
                channel="web", operator_id="operator", chat_id="queue-chat",
                message_id="m2", text="颜色再淡一点", chat_type="p2p",
            )
            ok2, _, second = await queue.enqueue("fix", msg2.text, msg2, port, self.runner)
            self.assertTrue(ok2)
            return second

        second = asyncio.run(scenario())
        self.assertTrue(second.refinement_intent)
        restarted = JobQueue()
        restarted.configure(self.settings, self.runner, recover=False)
        restored = asyncio.run(restarted.get_job(second.id))
        self.assertTrue(restored.refinement_intent)
        self.assertEqual(restored.session_id, stable_session_id("web", "queue-chat", "operator"))

    def test_apply_applies_accumulated_three_turn_diff_and_closes_chain(self):
        _, path, active = self._first()
        (path / "README.md").write_text("base\none\n", encoding="utf-8")
        _, path, active = self._continue("runtime-b", active)
        (path / "README.md").write_text("base\none\ntwo\n", encoding="utf-8")
        _, path, active = self._continue("runtime-c", active)
        (path / "README.md").write_text("base\none\ntwo\nthree\n", encoding="utf-8")

        result = asyncio.run(self.runner.apply_job("q3", path))
        self.assertIn("Applied q3", result)
        self.assertEqual(
            (self.repo / "README.md").read_text(encoding="utf-8"),
            "base\none\ntwo\nthree\n",
        )
        self.assertIsNone(self.store.active(self.session))
        conn = self.store._connect()
        try:
            row = conn.execute("SELECT state FROM session_worktrees WHERE id = ?", (active["id"],)).fetchone()
            self.assertEqual(row[0], APPLIED)
        finally:
            conn.close()

    def test_after_apply_next_execution_creates_new_chain_and_path(self):
        _, old_path, active = self._first()
        (old_path / "README.md").write_text("base\napplied\n", encoding="utf-8")
        asyncio.run(self.runner.apply_job("q1", old_path))
        _, new_path, new_active = self._first("runtime-next")
        self.assertNotEqual(old_path, new_path)
        self.assertNotEqual(active["id"], new_active["id"])

    def test_discard_removes_shared_worktree_and_closes_chain(self):
        _, path, active = self._first()
        _, path2, active = self._continue("runtime-b", active)
        self.assertEqual(path, path2)
        result = asyncio.run(self.runner.discard_job("q2", path))
        self.assertIn("Discarded", result)
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.active(self.session))
        conn = self.store._connect()
        try:
            row = conn.execute("SELECT state FROM session_worktrees WHERE id = ?", (active["id"],)).fetchone()
            self.assertEqual(row[0], DISCARDED)
        finally:
            conn.close()

    def test_stale_queued_refinement_fails_closed_instead_of_creating_new_worktree(self):
        queue = JobQueue()
        queue.configure(self.settings, self.runner, recover=False)
        msg = InboundMessage(
            channel="web", operator_id="operator", chat_id="stale-chat",
            message_id="m1", text="first", chat_type="p2p",
        )
        port = FakePort()

        async def prepare():
            _, _, first = await queue.enqueue("fix", "first", msg, port, self.runner)
            await queue.dequeue(require_idle=True)
            msg2 = InboundMessage(
                channel="web", operator_id="operator", chat_id="stale-chat",
                message_id="m2", text="继续", chat_type="p2p",
            )
            _, _, second = await queue.enqueue("fix", "继续", msg2, port, self.runner)
            return first, second

        _, second = asyncio.run(prepare())
        runtime = self._job("runtime-stale")
        queue.bind_runtime_job(second.id, runtime)
        self.assertTrue(getattr(runtime, "refinement_resolution_error", ""))
        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            asyncio.run(self.runner._create_worktree(runtime))
        self.assertFalse(self.runner._job_worktree_path(runtime).exists())

    def test_cancelled_or_failed_turn_does_not_close_active_chain(self):
        job, path, active = self._first()
        job.state = JobState.CANCELLED
        self.assertEqual(self.store.active(self.session)["id"], active["id"])
        job.state = JobState.FAILED
        self.assertEqual(self.store.active(self.session)["id"], active["id"])
        self.assertTrue(path.exists())

    def test_outside_worktree_path_fails_closed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(RuntimeError, "outside Conveyor"):
            asyncio.run(self.runner._validate_reuse_worktree(outside))

    def test_registered_worktree_from_other_repo_is_rejected(self):
        other = Path(self.temp.name) / "other-repo"
        other.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=other, check=True)
        subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=other, check=True)
        (other / "x.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "x.txt"], cwd=other, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=other, check=True, capture_output=True)
        injected = self.settings.codex_task_root / "worktrees" / "other"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(injected), "HEAD"],
            cwd=other, check=True, capture_output=True,
        )
        with self.assertRaisesRegex(RuntimeError, "different repository"):
            asyncio.run(self.runner._validate_reuse_worktree(injected))

    def test_refinement_feedback_routing_is_conservative(self):
        positives = [
            "右边还是太挤", "颜色再淡一点", "这个动画慢一点", "还是不对", "继续",
            "上面再留一点空白", "make it less rounded", "still too wide", "a little darker",
            "move it down a bit",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(is_refinement_feedback(text))
        negatives = ["为什么你刚才这么改？", "这些文件分别干嘛？", "这个方案有什么风险？"]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(is_refinement_feedback(text))

    def test_active_session_feedback_routes_but_other_session_does_not(self):
        self._first()
        active_msg = InboundMessage(
            channel="web", operator_id="operator", chat_id="chat-a",
            message_id="m1", text="右边还是太挤", chat_type="p2p",
        )
        other_msg = InboundMessage(
            channel="web", operator_id="operator", chat_id="chat-b",
            message_id="m2", text="右边还是太挤", chat_type="p2p",
        )
        question = InboundMessage(
            channel="web", operator_id="operator", chat_id="chat-a",
            message_id="m3", text="为什么你刚才这么改？", chat_type="p2p",
        )
        self.assertTrue(should_route_refinement(self.settings, active_msg))
        self.assertFalse(should_route_refinement(self.settings, other_msg))
        self.assertFalse(should_route_refinement(self.settings, question))

    def test_stable_session_identity_separates_channel_operator_and_chat(self):
        base = stable_session_id("web", "same", "operator-a")
        self.assertNotEqual(base, stable_session_id("telegram", "same", "operator-a"))
        self.assertNotEqual(base, stable_session_id("web", "same", "operator-b"))
        self.assertNotEqual(base, stable_session_id("web", "other", "operator-a"))


if __name__ == "__main__":
    unittest.main()
