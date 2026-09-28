from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel.types import InboundMessage
from handlers.job_queue import JobQueue
from refinement_store import RefinementStore, stable_session_id
from runner.operators.jobs import apply_job, discard_job
from runner.types import Job, JobMode
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
    is_codex = False

    async def reply(self, _msg, _text):
        return None


class RefinementRaceGuardTests(unittest.TestCase):
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
        self.session = stable_session_id("web", "chat-race", "operator")

    def tearDown(self) -> None:
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, check=False, capture_output=True)
        self.temp.cleanup()

    def _active_worktree(self):
        job = Job("runtime-root", JobMode.FIX, "initial", "danger-full-access")
        job.refinement_session_id = self.session
        job.refinement_channel = "web"
        job.refinement_operator_id = "operator"
        job.refinement_source_chat_id = "chat-race"
        path = asyncio.run(self.runner._create_worktree(job))
        active = self.store.active(self.session)
        self.assertIsNotNone(active)
        return path, active

    def _queue_followup(self, queue: JobQueue, text: str = "继续"):
        msg = InboundMessage(
            channel="web",
            operator_id="operator",
            chat_id="chat-race",
            message_id="followup",
            text=text,
            chat_type="p2p",
        )
        ok, _, queued = asyncio.run(
            queue.enqueue("fix", text, msg, FakePort(), self.runner)
        )
        self.assertTrue(ok)
        self.assertTrue(queued.refinement_intent)
        return queued

    def test_dirty_main_apply_rolls_back_guard_and_keeps_chain_active(self):
        path, active = self._active_worktree()
        (path / "README.md").write_text("base\nrefined\n", encoding="utf-8")
        (self.repo / "README.md").write_text("base\ndirty main\n", encoding="utf-8")

        result = asyncio.run(self.runner.apply_job("q-dirty", path))

        self.assertIn("dirty repo", result)
        recovered = self.store.active(self.session)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["id"], active["id"])
        self.assertTrue(path.exists())

    def test_running_refinement_blocks_apply_and_discard(self):
        path, active = self._active_worktree()
        (path / "README.md").write_text("base\nrefined\n", encoding="utf-8")
        queue = JobQueue()
        queue.configure(self.settings, self.runner, recover=False)
        queued = self._queue_followup(queue)
        running = asyncio.run(queue.dequeue(require_idle=True))
        self.assertEqual(running.id, queued.id)

        apply_result = asyncio.run(self.runner.apply_job("q-apply", path))
        discard_result = asyncio.run(self.runner.discard_job("q-discard", path))

        self.assertIn("still running", apply_result)
        self.assertIn("still running", discard_result)
        self.assertEqual(self.store.active(self.session)["id"], active["id"])
        self.assertTrue(path.exists())

    def test_refinement_state_failure_blocks_apply_and_discard(self):
        path, active = self._active_worktree()
        (path / "README.md").write_text("base\nrefined\n", encoding="utf-8")

        with patch(
            "refinement_store.RefinementStore.active_for_worktree",
            side_effect=RuntimeError("control DB unavailable"),
        ):
            apply_result = asyncio.run(self.runner.apply_job("q-apply", path))
            discard_result = asyncio.run(self.runner.discard_job("q-discard", path))

        self.assertIn("Refinement state is unavailable", apply_result)
        self.assertIn("Refinement state is unavailable", discard_result)
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertTrue(path.exists())
        self.assertEqual(self.store.active(self.session)["id"], active["id"])

    def test_queued_followup_after_discard_fails_closed_on_start(self):
        path, _ = self._active_worktree()
        queue = JobQueue()
        queue.configure(self.settings, self.runner, recover=False)
        queued = self._queue_followup(queue, "颜色再淡一点")

        result = asyncio.run(self.runner.discard_job("q-discard", path))
        self.assertIn("Discarded", result)
        self.assertFalse(path.exists())

        claimed = asyncio.run(queue.dequeue(require_idle=True))
        self.assertEqual(claimed.id, queued.id)
        runtime = Job("runtime-stale", JobMode.FIX, claimed.prompt, "danger-full-access")
        queue.bind_runtime_job(claimed.id, runtime)
        self.assertIn("no longer available", runtime.refinement_resolution_error)
        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            asyncio.run(self.runner._create_worktree(runtime))
        self.assertFalse(self.runner._job_worktree_path(runtime).exists())


if __name__ == "__main__":
    unittest.main()
