"""Fault-injection regression tests for Apply and cross-channel tool approvals."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import approval_relay
from channel.types import InboundMessage
from handlers.tools.confirm import clear_all_pending, create_pending, get_pending
from handlers.tools.runner import execute_confirmed
from refinement_store import RefinementStore, stable_session_id
from runner.operators.jobs import apply_job
from runner.types import Job, JobMode
from runner.worktree import _copy_validated_untracked_files, _create_worktree, _git, _job_worktree_path, _remove_worktree, _validate_reuse_worktree


class Runner:
    _git = _git
    _job_worktree_path = _job_worktree_path
    _create_worktree = _create_worktree
    _validate_reuse_worktree = _validate_reuse_worktree
    _remove_worktree = _remove_worktree
    _copy_validated_untracked_files = _copy_validated_untracked_files
    apply_job = apply_job

    def __init__(self, settings):
        self.settings = settings
        self.current_job = self.last_job = None


class Port:
    def __init__(self):
        self.replies = []

    async def reply(self, _msg, text):
        self.replies.append(text)
        return "reply-id"


class AtomicApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        self.settings = SimpleNamespace(
            codex_workspace_root=self.repo.resolve(),
            codex_task_root=(root / "tasks").resolve(),
            codex_memory_root=(root / "memory").resolve(),
            conveyor_max_worktrees_bytes=1024 * 1024 * 1024,
            conveyor_apply_allow_high_risk=False,
            conveyor_apply_max_untracked_bytes=1024 * 1024,
            user_timezone="UTC",
        )
        (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
        self.runner = Runner(self.settings)
        self.store = RefinementStore(self.settings)
        self.session = stable_session_id("web", "test", "operator")
        job = Job("runtime-atomic", JobMode.FIX, "update", "danger-full-access")
        job.refinement_session_id = self.session
        job.refinement_channel = "web"
        job.refinement_operator_id = "operator"
        job.refinement_source_chat_id = "test"
        self.worktree = asyncio.run(self.runner._create_worktree(job))
        self.assertIsNotNone(self.store.active(self.session))

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, check=False, capture_output=True)

    def _write_patch(self):
        (self.worktree / "README.md").write_text("changed\n")
        (self.worktree / "docs").mkdir(exist_ok=True)
        (self.worktree / "docs" / "NEW.txt").write_text("new file\n")

    def _assert_rolled_back(self):
        self.assertEqual((self.repo / "README.md").read_text(), "base\n")
        self.assertFalse((self.repo / "docs" / "NEW.txt").exists())
        self.assertEqual(subprocess.check_output(["git", "status", "--porcelain"], cwd=self.repo).strip(), b"")
        self.assertIsNotNone(self.store.active(self.session))

    def test_tracked_patch_reversed_after_untracked_copy_failure(self):
        self._write_patch()
        with patch.object(Runner, "_copy_validated_untracked_files", side_effect=OSError("disk full")):
            result = asyncio.run(self.runner.apply_job("q1", self.worktree))
        self.assertIn("Rollback", result)
        self._assert_rolled_back()

    def test_partial_copy_cleaned_even_if_copy2_writes_then_errors(self):
        self._write_patch()
        def partial_copy(_source, target):
            Path(target).write_bytes(b"partial")
            raise OSError("simulated disk full")
        with patch("runner.worktree.shutil.copy2", side_effect=partial_copy):
            result = asyncio.run(self.runner.apply_job("q2", self.worktree))
        self.assertIn("Rollback", result)
        self._assert_rolled_back()

    def test_untracked_path_symlink_parent_blocked(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "note.txt").write_text("note")
        (self.worktree / "docs").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "outside worktree"):
            asyncio.run(self.runner._copy_validated_untracked_files(self.worktree, ["docs/note.txt"]))
        self.assertEqual((outside / "note.txt").read_text(), "note")


class RelayFailClosedTests(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = SimpleNamespace(
            approval_relay_enabled=True, approval_inbox_enabled=True,
            approval_relay_db=root / "relay.db", codex_memory_root=root / "memory",
            codex_task_root=root / "tasks",
        )
        self.msg = InboundMessage(channel="telegram", operator_id="op", chat_id="chat",
                                  message_id=None, text="确认")
        self.port = Port()

    def tearDown(self):
        clear_all_pending()

    def test_missing_published_row_refuses_execution_and_preserves_action(self):
        pending = create_pending("notes.add", "hello", "op", "chat", "telegram")
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as execute:
            asyncio.run(execute_confirmed(self.msg, self.port, self.settings, pending.token))
            execute.assert_not_awaited()
        self.assertIsNotNone(get_pending(pending.token))
        self.assertIn("不可验证", self.port.replies[-1])

    def test_database_io_failure_refuses_execution_and_can_retry(self):
        pending = create_pending("notes.add", "hello", "op", "chat", "telegram")
        self.assertTrue(approval_relay.publish(self.settings, pending, summary="note"))
        with patch("approval_relay._connect", side_effect=OSError("database unavailable")):
            with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as execute:
                asyncio.run(execute_confirmed(self.msg, self.port, self.settings, pending.token))
                execute.assert_not_awaited()
        self.assertIsNotNone(get_pending(pending.token))
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock, return_value="ok") as execute:
            asyncio.run(execute_confirmed(self.msg, self.port, self.settings, pending.token))
            execute.assert_awaited_once()
        self.assertIsNone(get_pending(pending.token))

    def test_expired_relay_row_refuses_even_if_local_action_pending(self):
        pending = create_pending("notes.add", "hello", "op", "chat", "telegram")
        self.assertTrue(approval_relay.publish(self.settings, pending, summary="note"))
        conn = approval_relay._connect(self.settings)
        try:
            with conn:
                conn.execute("UPDATE relay_approvals SET expires_at=0 WHERE token=?", (pending.token,))
        finally:
            conn.close()
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as execute:
            asyncio.run(execute_confirmed(self.msg, self.port, self.settings, pending.token))
            execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
