"""Nightly repair runs host git on a worktree that Pi had read-write in its
sandbox. These use real git (no mocks) to show Pi's changes cannot execute
commands on the host or slip past the scope rules."""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import overnight_repair
from overnight_process import command as real_command
from overnight_repair import PiRepair


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class _Workspaces:
    def __init__(self, repo: Path, root: Path):
        self.repo, self.root = repo, root

    @contextlib.asynccontextmanager
    async def detached(self, sha):
        path = self.root / "wt"
        git("worktree", "add", "--detach", str(path), sha, cwd=self.repo)
        try:
            yield path
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=self.repo,
                           capture_output=True)


class _GitHub:
    def _json(self, *args):
        return []


@unittest.skipUnless(shutil.which("git"), "git required")
class RepairGitHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.repo = root / "origin.git", root / "repo"
        git("init", "-q", "--bare", "-b", "preview", str(self.origin), cwd=root)
        git("clone", "-q", str(self.origin), str(self.repo), cwd=root)
        for rel, text in {"web/src/app.ts": "x\n", ".github/workflows/sec.yml": "on: push\n"}.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        git("add", "-A", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:preview", cwd=self.repo)
        self.pwned = root / "PWNED"
        self.repair = PiRepair(_Workspaces(self.repo, root), _GitHub(), pi="fake-pi")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, pi_effect):
        async def fake_command(argv, **kw):
            if argv and argv[0] == "fake-pi":
                pi_effect(Path(kw["cwd"]))
                return ""
            if argv[:2] == ["git", "push"]:
                return ""
            return await real_command(argv, **kw)

        async def no_publish(*a, **k):
            return {"pushed": True}

        with patch.object(overnight_repair, "command", side_effect=fake_command), \
                patch.object(PiRepair, "_publish", new=no_publish):
            return asyncio.run(self.repair({"issue": {"number": 7}, "finding": {}}))

    def test_rewritten_git_pointer_cannot_run_host_commands(self):
        def pi(path):
            # Plant a complete git dir with a hostile config and hook, then point
            # the worktree's .git file at it (verified to run both on git 2.43).
            evil = path / "evil"
            shutil.copytree(self.repo / ".git", evil)
            shutil.rmtree(evil / "worktrees", ignore_errors=True)
            git("config", "core.bare", "false", cwd=evil)
            git("config", "core.fsmonitor", f"touch {self.pwned} #", cwd=evil)
            hook = evil / "hooks" / "pre-commit"
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {self.pwned}\n")
            hook.chmod(0o755)
            (path / ".git").write_text("gitdir: evil\n")
            (path / "web/src/app.ts").write_text("y\n")

        with self.assertRaises(ValueError):
            self._run(pi)
        self.assertFalse(self.pwned.exists(), "planted fsmonitor ran on the host")

    def test_moving_a_workflow_into_scope_is_refused(self):
        def pi(path):
            (path / ".github/workflows/sec.yml").rename(path / "web/src/sec.yml")

        with self.assertRaisesRegex(ValueError, "outside workbench scope|delete"):
            self._run(pi)

    def test_symlink_is_refused(self):
        def pi(path):
            (path / "web/src/link").symlink_to("/etc/passwd")

        with self.assertRaisesRegex(ValueError, "symlink"):
            self._run(pi)

    def test_failed_pi_leaves_no_branch_behind(self):
        def pi(path):
            raise RuntimeError("pi crashed")

        with self.assertRaises(RuntimeError):
            self._run(pi)
        refs = git("for-each-ref", "--format=%(refname)", "refs/heads/codex/", cwd=self.repo)
        self.assertEqual(refs.strip(), "")

    def test_in_scope_change_is_committed(self):
        def pi(path):
            (path / "web/src/app.ts").write_text("fixed\n")

        self.assertEqual(self._run(pi), {"pushed": True})
        log = git("log", "-1", "--format=%s", "codex/nightly-issue-7-attempt-1", cwd=self.repo)
        self.assertEqual(log.strip(), "Fix preview issue #7")


if __name__ == "__main__":
    unittest.main()
