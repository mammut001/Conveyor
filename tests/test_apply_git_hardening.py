"""/apply and /discard against real git: paths the policy must see, and
directories /discard must never delete."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runner.apply_policy import collect_tracked_changed_files


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@unittest.skipUnless(shutil.which("git"), "git required")
class TrackedChangeCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", cwd=self.repo)
        for rel in ("security/guard.py", "docs/readme.md", ".github/workflows/ci.yml"):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("x\n")
        git("add", "-A", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rename_reports_the_deleted_protected_path(self):
        git("mv", "security/guard.py", "docs/guard.py", cwd=self.repo)
        result = collect_tracked_changed_files(self.repo)
        self.assertTrue(result.ok)
        self.assertIn("security/guard.py", result.paths)
        self.assertIn("docs/guard.py", result.paths)

    def test_non_ascii_path_is_not_quoted(self):
        (self.repo / ".github/workflows/ci.yml").write_text("changed\n")
        name = ".github/workflows/é.yml"
        (self.repo / name).write_text("on: push\n")
        git("add", "-A", cwd=self.repo)
        result = collect_tracked_changed_files(self.repo)
        self.assertIn(name, result.paths)
        self.assertFalse(any(p.startswith('"') for p in result.paths))

    def test_staged_symlink_is_refused(self):
        (self.repo / "docs/etc_link").symlink_to("/etc")
        git("add", "-A", cwd=self.repo)
        result = collect_tracked_changed_files(self.repo)
        self.assertFalse(result.ok)
        self.assertIn("symlink", result.error)


class DiscardGuardTest(unittest.TestCase):
    def test_remove_refuses_paths_outside_the_worktrees_dir(self):
        from runner.worktree import _remove_worktree
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"; workspace.mkdir()
            (workspace / "keep.txt").write_text("precious\n")
            runner = SimpleNamespace(settings=SimpleNamespace(codex_task_root=root / "tasks"))

            async def no_git(*a, **k):
                raise AssertionError("git must not run for a refused path")
            runner._git = no_git
            for target in (workspace, root / "tasks" / "worktrees", root / "tasks" / "worktrees" / ".." / ".."):
                with self.subTest(target=str(target)):
                    with self.assertRaises(RuntimeError):
                        asyncio.run(_remove_worktree(runner, target))
            self.assertTrue((workspace / "keep.txt").exists())

@unittest.skipUnless(shutil.which("git"), "git required")
class WorktreeOwnershipTest(unittest.TestCase):
    def test_worktree_belongs_only_to_its_own_repository(self):
        from runner.worktree import _worktree_belongs_to
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("a", "b"):
                (root / name).mkdir()
                git("init", "-q", cwd=root / name)
                (root / name / "f").write_text("x\n")
                git("add", "-A", cwd=root / name)
                git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i", cwd=root / name)
            git("worktree", "add", "-q", "--detach", str(root / "wt"), "HEAD", cwd=root / "a")

            async def real_git(args, cwd=None, **_k):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
            runner = SimpleNamespace(_git=real_git)
            self.assertTrue(asyncio.run(_worktree_belongs_to(runner, root / "wt", root / "a")))
            self.assertFalse(asyncio.run(_worktree_belongs_to(runner, root / "wt", root / "b")))

class AutoRunFilesPolicyTest(unittest.TestCase):
    def test_git_hooks_and_direnv_files_are_high_risk(self):
        from config import load_settings
        from runner.apply_policy import validate_apply_paths
        settings = load_settings(".env.test.example")
        for path in (".husky/pre-commit", ".githooks/post-checkout", ".envrc", "web/.envrc",
                     ".pre-commit-config.yaml"):
            with self.subTest(path=path):
                self.assertFalse(validate_apply_paths([path], kind="tracked", settings=settings).allowed)


if __name__ == "__main__":
    unittest.main()
