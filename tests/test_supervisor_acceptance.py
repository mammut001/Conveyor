"""Acceptance must describe the workspace actually reviewed."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_supervisor as supervisor


class SupervisorAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "tracked.txt").write_text("original")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.settings = SimpleNamespace(codex_memory_root=self.root / "state", codex_model=None)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def flow(self, mutate_review=False):
        repo = self.repo

        class Executor:
            def available(self):
                return True, "ok"

            async def run(self, *, role, prompt, worktree_path, timeout_seconds):
                if role.name == "implementer":
                    (repo / "new.txt").write_text("implemented")
                if role.name == "reviewer":
                    if mutate_review:
                        (repo / "tracked.txt").write_text("changed during review")
                    return supervisor.ExecutionResult(success=True, output="PASS")
                return supervisor.ExecutionResult(success=True, output="planned")

        task = supervisor.create_task(self.settings, goal="add file", repo_path=str(repo))
        with patch.object(supervisor, "get_executor", return_value=Executor()):
            result = asyncio.run(supervisor.run_supervisor_flow(
                self.settings, task["id"], goal="add file", worktree_path=str(repo)))
        return result

    def test_edited_or_untracked_changes_invalidate_acceptance(self):
        task = self.flow()
        self.assertEqual(task["state"], "completed")
        self.assertIsNotNone(supervisor.effective_acceptance(self.settings, task["id"]))
        (self.repo / "new.txt").write_text("changed")
        self.assertIsNone(supervisor.effective_acceptance(self.settings, task["id"]))
        (self.repo / "new.txt").write_text("implemented")
        (self.repo / "tracked.txt").write_text("changed")
        self.assertIsNone(supervisor.effective_acceptance(self.settings, task["id"]))

    def test_review_cannot_accept_workspace_changed_during_review(self):
        task = self.flow(mutate_review=True)
        self.assertEqual(task["state"], "failed")
        self.assertIn("changed during review", task["error"])
        self.assertIsNone(supervisor.latest_acceptance(self.settings, task["id"]))

    def test_legacy_sha_only_acceptance_is_not_effective(self):
        task = supervisor.create_task(self.settings, goal="test", repo_path=str(self.repo),
                                      worktree_path=str(self.repo), base_sha="old")
        supervisor.record_acceptance(self.settings, task["id"], sha="old", passed=True)
        self.assertIsNone(supervisor.effective_acceptance(self.settings, task["id"]))


if __name__ == "__main__":
    unittest.main()
