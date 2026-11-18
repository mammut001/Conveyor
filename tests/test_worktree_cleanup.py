from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conveyor.core.config import load_settings
from conveyor.core.refinement_store import RefinementStore
from runner import CodexRunner
from runner.types import Job, JobMode, JobState
from conveyor.entrypoints.web_console import validate_codex_bin


class WorktreeCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        env_patch = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": "12345",
            "CODEX_WORKSPACE_ROOT": str(self.repo.resolve()),
            "CODEX_TASK_ROOT": str((self.root / "tasks").resolve()),
            "CODEX_MEMORY_ROOT": str((self.root / "memory").resolve()),
        }
        with patch.dict(os.environ, env_patch):
            base = load_settings()
        self.settings = dataclasses.replace(
            base,
            codex_workspace_root=self.repo.resolve(),
            codex_task_root=(self.root / "tasks").resolve(),
            codex_memory_root=(self.root / "memory").resolve(),
            codex_retry_429_delays_seconds=[],
        )
        (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
        (self.settings.codex_task_root / "logs").mkdir(parents=True, exist_ok=True)
        (self.settings.codex_task_root / "locks").mkdir(parents=True, exist_ok=True)
        self.runner = CodexRunner(self.settings)

    def tearDown(self) -> None:
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, check=False, capture_output=True)
        self.tmp.cleanup()

    def _make_job(self, job_id: str, *, mode: JobMode = JobMode.RUN, prompt: str = "test") -> Job:
        job = Job(id=job_id, mode=mode, prompt=prompt, sandbox=mode.sandbox)
        logs_dir = self.settings.codex_task_root / "logs" / job.id
        logs_dir.mkdir(parents=True, exist_ok=True)
        job.metadata_path = logs_dir / "job.json"
        return job

    async def _dummy_progress(self, msg: str) -> None:
        pass

    async def test_codex_process_start_raises_cleans_up_worktree(self) -> None:
        job = self._make_job("job-fail-start")
        wt_path = self.runner._job_worktree_path(job)

        with patch.object(self.runner, "_run_codex_attempt", side_effect=RuntimeError("exec failed")):
            await self.runner._run_job(job, self._dummy_progress)

        self.assertEqual(job.state, JobState.FAILED)
        self.assertIsNone(job.worktree_path)
        self.assertFalse(wt_path.exists())

        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(str(wt_path), res.stdout)

    async def test_cancelled_job_without_changes_cleans_up_worktree(self) -> None:
        job = self._make_job("job-cancel-start")
        job.cancel_requested = True
        wt_path = self.runner._job_worktree_path(job)

        await self.runner._run_job(job, self._dummy_progress)

        self.assertEqual(job.state, JobState.CANCELLED)
        self.assertIsNone(job.worktree_path)
        self.assertFalse(wt_path.exists())

        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(str(wt_path), res.stdout)

    async def test_failed_job_with_changes_keeps_worktree(self) -> None:
        job = self._make_job("job-fail-changes")
        wt_path = self.runner._job_worktree_path(job)

        async def fake_attempt(j: Job, on_progress) -> None:
            assert j.worktree_path is not None
            (j.worktree_path / "scratch.txt").write_text("hello", encoding="utf-8")
            j.error = "failed with changes"
            j.return_code = 1

        with patch.object(self.runner, "_run_codex_attempt", side_effect=fake_attempt):
            await self.runner._run_job(job, self._dummy_progress)

        self.assertEqual(job.state, JobState.FAILED)
        self.assertIsNotNone(job.worktree_path)
        self.assertTrue(wt_path.exists())
        self.assertTrue((wt_path / "scratch.txt").exists())

        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(str(wt_path), res.stdout)

    async def test_reused_refinement_worktree_failure_not_removed(self) -> None:
        reuse_wt = (self.settings.codex_task_root / "worktrees" / "reused-refinement").resolve()
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(reuse_wt), "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.assertTrue(reuse_wt.exists())

        job = self._make_job("job-reuse-fail")
        job.reuse_worktree_path = reuse_wt
        job.reused_worktree = True
        job.worktree_created = False
        job.worktree_path = reuse_wt

        async def fake_create_worktree(j: Job) -> Path:
            j.worktree_path = reuse_wt
            j.worktree_created = False
            j.reused_worktree = True
            return reuse_wt

        with patch.object(self.runner, "_create_worktree", side_effect=fake_create_worktree), \
             patch.object(self.runner, "_run_codex_attempt", side_effect=RuntimeError("fail")):
            await self.runner._run_job(job, self._dummy_progress)

        self.assertEqual(job.state, JobState.FAILED)
        self.assertTrue(reuse_wt.exists())

    async def test_reconcile_orphans_lifecycle(self) -> None:
        worktrees_dir = self.settings.codex_task_root / "worktrees"
        now = time.time()
        old_time = now - 48 * 3600

        orphan_dir = worktrees_dir / "orphan-job-123"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        self._make_git_worktree(orphan_dir)
        os.utime(orphan_dir, (old_time, old_time))

        young_dir = worktrees_dir / "young-job-456"
        young_dir.mkdir(parents=True, exist_ok=True)
        os.utime(young_dir, (now, now))
        self._make_git_worktree(young_dir)

        daily_dir = worktrees_dir / "day-2026-01-01"
        daily_dir.mkdir(parents=True, exist_ok=True)
        self._make_git_worktree(daily_dir)
        os.utime(daily_dir, (old_time, old_time))

        # The Supervisor keeps all managed worktrees inside this container
        # directory, so it must never be treated as a single orphan.
        supervisor_container = worktrees_dir / "supervisor"
        supervisor_container.mkdir(parents=True, exist_ok=True)
        supervisor_wt = supervisor_container / "wt-abc"
        supervisor_wt.mkdir(parents=True, exist_ok=True)
        self._make_git_worktree(supervisor_wt)
        os.utime(supervisor_wt, (old_time, old_time))
        os.utime(supervisor_container, (old_time, old_time))
        self.assertFalse((supervisor_container / ".git").exists())

        # A stray old folder that is not a git worktree is never removed.
        stray_dir = worktrees_dir / "stray-not-a-worktree"
        stray_dir.mkdir(parents=True, exist_ok=True)
        os.utime(stray_dir, (old_time, old_time))
        self.assertFalse((stray_dir / ".git").exists())

        # A stray old folder holding a non-git payload is never removed either.
        stray_with_file = worktrees_dir / "stray-with-file"
        stray_with_file.mkdir(parents=True, exist_ok=True)
        (stray_with_file / "data.txt").write_text("x", encoding="utf-8")
        os.utime(stray_with_file, (old_time, old_time))

        refinement_dir = (worktrees_dir / "active-refinement-wt").resolve()
        refinement_dir.mkdir(parents=True, exist_ok=True)
        self._make_git_worktree(refinement_dir)
        os.utime(refinement_dir, (old_time, old_time))
        store = RefinementStore(self.settings)
        store.bind_new(
            session_id="web:session-1",
            channel="web",
            operator_id="op1",
            source_chat_id="chat-1",
            worktree_path=refinement_dir,
            runtime_job_id="job-active-1",
            queue_job_id="queue-1",
        )

        # An old worktree still referenced by a job's metadata (e.g. a
        # completed job awaiting /apply or /discard) is not an orphan.
        referenced_dir = (worktrees_dir / "job-completed-789").resolve()
        referenced_dir.mkdir(parents=True, exist_ok=True)
        self._make_git_worktree(referenced_dir)
        referenced_job = self._make_job("job-completed-789")
        referenced_job.state = JobState.COMPLETED
        referenced_job.worktree_path = referenced_dir
        self.runner._write_job_metadata(referenced_job)
        os.utime(referenced_dir, (old_time, old_time))

        dry_result = await self.runner.reconcile_orphans(dry_run=True, ttl_seconds=24 * 3600)
        self.assertNotIn(str(referenced_dir), dry_result["orphans"])
        self.assertTrue(dry_result["dry_run"])
        self.assertEqual(dry_result["removed"], [])
        self.assertIn(str(orphan_dir), dry_result["orphans"])
        self.assertNotIn(str(young_dir), dry_result["orphans"])
        self.assertNotIn(str(daily_dir), dry_result["orphans"])
        self.assertNotIn(str(refinement_dir), dry_result["orphans"])
        # (a) the supervisor container is never an orphan, even when it holds
        # an expired worktree inside it.
        self.assertNotIn(str(supervisor_container), dry_result["orphans"])
        self.assertNotIn(str(supervisor_wt), dry_result["orphans"])
        # (b) a directory without a .git entry is not a worktree: not an orphan.
        self.assertNotIn(str(stray_dir), dry_result["orphans"])
        self.assertNotIn(str(stray_with_file), dry_result["orphans"])
        # (c) the orphan job worktree is still found (and removed below).
        self.assertIn(str(orphan_dir), dry_result["orphans"])
        self.assertTrue(orphan_dir.exists())
        self.assertTrue(supervisor_container.exists())
        self.assertTrue(supervisor_wt.exists())
        self.assertTrue(stray_dir.exists())
        self.assertTrue(stray_with_file.exists())

        non_dry_result = await self.runner.reconcile_orphans(dry_run=False, ttl_seconds=24 * 3600)
        self.assertFalse(non_dry_result["dry_run"])
        self.assertIn(str(orphan_dir), non_dry_result["removed"])
        self.assertFalse(orphan_dir.exists())
        self.assertTrue(young_dir.exists())
        self.assertTrue(daily_dir.exists())
        self.assertTrue(refinement_dir.exists())
        self.assertTrue(referenced_dir.exists())
        self.assertTrue(supervisor_container.exists())
        self.assertTrue(supervisor_wt.exists())
        self.assertTrue(stray_dir.exists())
        self.assertTrue(stray_with_file.exists())
        self.assertEqual(non_dry_result["removed"], [str(orphan_dir)])

    def _make_git_worktree(self, path: Path) -> None:
        """Give *path* a ``.git`` entry so it models a real git worktree.

        Linked worktrees carry a ``.git`` *file*; the main checkout carries a
        ``.git`` *directory*. Both count as "is a worktree".
        """
        (path / ".git").write_text(
            "gitdir: /tmp/nonexistent/.git/worktrees/x\n", encoding="utf-8"
        )

    def test_validate_codex_bin(self) -> None:
        settings_ok = SimpleNamespace(codex_bin=sys.executable)
        validate_codex_bin(settings_ok)

        settings_path = SimpleNamespace(codex_bin="git")
        validate_codex_bin(settings_path)

        settings_missing_path = SimpleNamespace(codex_bin="/path/to/missing_codex_bin_12345")
        with self.assertRaises(RuntimeError) as ctx1:
            validate_codex_bin(settings_missing_path)
        self.assertIn("CODEX_BIN not found: /path/to/missing_codex_bin_12345", str(ctx1.exception))

        settings_missing_name = SimpleNamespace(codex_bin="missing_codex_bin_99999")
        with self.assertRaises(RuntimeError) as ctx2:
            validate_codex_bin(settings_missing_name)
        self.assertIn("CODEX_BIN not found: missing_codex_bin_99999", str(ctx2.exception))


if __name__ == "__main__":
    unittest.main()
