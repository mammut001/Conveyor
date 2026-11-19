"""The polled job list reuses a finished job's `git status` for a short time."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conveyor.web.web_control import WebControl


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_CONFIG_GLOBAL": "/dev/null", "PATH": "/usr/bin:/bin",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


class ChangedFilesCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.worktree = root / "tasks" / "worktrees" / "job1"
        self.worktree.mkdir(parents=True)
        _git(self.worktree, "init", "-q")
        (self.worktree / "a.txt").write_text("a\n")
        self.control = WebControl.__new__(WebControl)
        self.control.settings = SimpleNamespace(codex_task_root=str(root / "tasks"))
        self.git_calls = 0
        real_run = subprocess.run

        def counting_run(*args, **kwargs):
            if args and args[0][:2] == ["git", "status"]:
                self.git_calls += 1
            return real_run(*args, **kwargs)

        patcher = mock.patch("subprocess.run", side_effect=counting_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch("conveyor.core.subprocess_env.utility_env", return_value={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"})
        env.start()
        self.addCleanup(env.stop)

    def job(self, state: str) -> dict:
        return {"id": "job1", "state": state, "metadata": {"worktree_path": str(self.worktree)}}

    def test_finished_job_reuses_status_until_ttl(self) -> None:
        first = self.control._changed_files(self.job("completed"), cached=True)
        (self.worktree / "b.txt").write_text("b\n")
        second = self.control._changed_files(self.job("completed"), cached=True)
        self.assertEqual(self.git_calls, 1)
        self.assertEqual(first, second)
        self.assertEqual([f["path"] for f in second], ["a.txt"])
        with mock.patch("time.monotonic", return_value=10**9):
            third = self.control._changed_files(self.job("completed"), cached=True)
        self.assertEqual(self.git_calls, 2)
        self.assertEqual(sorted(f["path"] for f in third), ["a.txt", "b.txt"])

    def test_running_job_and_uncached_calls_stay_live(self) -> None:
        self.control._changed_files(self.job("running"), cached=True)
        self.control._changed_files(self.job("running"), cached=True)
        self.control._changed_files(self.job("completed"))
        self.control._changed_files(self.job("completed"))
        self.assertEqual(self.git_calls, 4)

    def test_decide_approval_clears_the_cache(self) -> None:
        self.control._changed_files(self.job("completed"), cached=True)
        (self.worktree / "b.txt").write_text("b\n")

        async def fake_decide(approval_id, approve):
            return {"ok": True}

        with mock.patch.object(self.control, "_decide_approval", side_effect=fake_decide):
            asyncio.run(self.control.decide_approval("x", True))
        files = self.control._changed_files(self.job("completed"), cached=True)
        self.assertEqual(self.git_calls, 2)
        self.assertEqual(sorted(f["path"] for f in files), ["a.txt", "b.txt"])


if __name__ == "__main__":
    unittest.main()
