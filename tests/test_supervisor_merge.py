"""tests/test_supervisor_merge.py — owner-confirmed 「合并到主分支」.

Real temp git repos, a real managed worktree created through
`supervisor_service.create_managed_worktree`, and a real merge into the main
checkout. No model API or CLI is ever called.

Covers: the happy path (uncommitted work is committed as `Conveyor`, then merged
with `--no-ff`, the merge commit has two parents, the main checkout is clean
afterwards), every blocker (busy task / running goal, wrong branch, dirty main,
unsafe repo config incl. `filter.*.smudge` and `core.hooksPath`, `.gitattributes`
driver), conflict → aborted with the main HEAD untouched, `nothing_to_merge`,
hooks never executed, the preview payload, and the HTTP routes.
"""
from __future__ import annotations

import dataclasses
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_supervisor as ags
import supervisor_goals as goals
import supervisor_merge as merge_mod
import supervisor_service as svc
from config import Settings

_ENV_KEYS_TO_CLEAR = (
    "CONVEYOR_AGENT_SUPERVISOR_ENABLED",
    "CONVEYOR_SUPERVISOR_ENABLED",
    "CONVEYOR_MAX_WORKTREES_BYTES",
)


def _make_settings(root: Path) -> Settings:
    workspace = root / "workspace"
    task_root = root / "tasks"
    mem_root = root / "memory"
    for d in (workspace, task_root, mem_root):
        d.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=1,
        codex_workspace_root=workspace,
        codex_bin="/bin/echo",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=5,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem_root,
        user_timezone="UTC",
        conveyor_web_enabled=True,
        conveyor_web_host="127.0.0.1",
        conveyor_web_port=0,
        conveyor_web_token="merge-test-token-0123456789-abcdefghijklmnop",
        chat_mode="off",
        chat_base_url=None,
        chat_api_key=None,
        chat_model=None,
        chat_tools_enabled=False,
        agent_supervisor_enabled=True,
        agent_supervisor_analyst_executor="fake",
        agent_supervisor_implementer_executor="fake",
        agent_supervisor_reviewer_executor="fake",
        agent_supervisor_max_repair_rounds=1,
        agent_supervisor_task_timeout_seconds=25,
        agent_supervisor_validation_commands=(),
    )


def _git(worktree: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _head(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").strip()


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Supervisor Test")
    (repo_dir / "README.md").write_text("# Disposable Merge Repo\n", encoding="utf-8")
    (repo_dir / "tracked.txt").write_text("tracked baseline\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "initial commit")
    return _head(repo_dir)


class _FakeHandler:
    """Minimal WebConsoleHandler stand-in for direct route dispatch tests."""

    def __init__(self, settings: Any) -> None:
        self.server = types.SimpleNamespace(
            control=types.SimpleNamespace(settings=settings),
            loop=None,
        )
        self.response: tuple[int, dict[str, Any]] | None = None

    def _json(self, status: Any, payload: dict[str, Any]) -> None:
        self.response = (int(status), payload)


class _MergeFixture(unittest.TestCase):
    """One real repo (main checkout) + one real managed Supervisor worktree."""

    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-merge-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="goal-calc")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = Path(str(wt["path"]))
        self.main_worktree_id = next(
            str(w["id"]) for w in repo["worktrees"] if w["is_main"]
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _preview(self, worktree_id: str | None = None) -> dict[str, Any]:
        return merge_mod.build_merge_preview(
            self.settings, worktree_id or self.worktree_id
        )

    def _merge(self, body: Any, worktree_id: str | None = None) -> dict[str, Any]:
        return merge_mod.merge_worktree(self.settings, worktree_id or self.worktree_id, body)

    def _blocker_codes(self, worktree_id: str | None = None) -> list[str]:
        return [b["code"] for b in self._preview(worktree_id)["blockers"]]

    def _worktree_change_uncommitted(self) -> None:
        """The realistic end state of a finished goal: nothing committed yet."""
        (self.wt_path / "tracked.txt").write_text(
            "tracked baseline\nworktree side\n", encoding="utf-8"
        )
        (self.wt_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    def _worktree_change_committed(self, message: str = "candidate work") -> None:
        self._worktree_change_uncommitted()
        _git(self.wt_path, "add", "-A")
        _git(self.wt_path, "commit", "-qm", message)

    def _set_config(self, key: str, value: str) -> None:
        _git(self.repo_dir, "config", key, value)

    def _unset_config(self, key: str) -> None:
        _git(self.repo_dir, "config", "--unset", key)

    def _install_hook(self, name: str, marker: Path) -> None:
        hook = self.repo_dir / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class MergePreviewTests(_MergeFixture):
    def test_preview_payload_for_a_finished_goal(self) -> None:
        self._worktree_change_uncommitted()
        payload = self._preview()

        self.assertEqual(
            set(payload),
            {
                "worktree_id", "repo_id", "source_branch", "target_branch",
                "can_merge", "blockers", "files", "commits_ahead", "has_uncommitted",
            },
        )
        self.assertEqual(payload["worktree_id"], self.worktree_id)
        self.assertEqual(payload["repo_id"], self.repo_id)
        self.assertEqual(payload["source_branch"], "goal-calc")
        self.assertEqual(payload["target_branch"], "main")
        self.assertTrue(payload["can_merge"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["commits_ahead"], 0)
        self.assertTrue(payload["has_uncommitted"])

        files = {entry["path"]: entry for entry in payload["files"]}
        self.assertEqual(files["tracked.txt"]["status"], "M")
        self.assertGreaterEqual(files["tracked.txt"]["additions"], 1)
        self.assertEqual(files["calc.py"]["status"], "?")
        for entry in payload["files"]:
            self.assertEqual(
                set(entry), {"path", "status", "additions", "deletions"}, entry
            )

    def test_preview_counts_commits_ahead(self) -> None:
        self._worktree_change_committed()
        payload = self._preview()
        self.assertEqual(payload["commits_ahead"], 1)
        self.assertFalse(payload["has_uncommitted"])
        self.assertTrue(payload["can_merge"])
        self.assertEqual(
            {entry["path"] for entry in payload["files"]}, {"tracked.txt", "calc.py"}
        )

    def test_preview_is_read_only(self) -> None:
        self._worktree_change_uncommitted()
        main_head = _head(self.repo_dir)
        main_status = _git(self.repo_dir, "status", "--porcelain")
        wt_head = _head(self.wt_path)
        wt_status = _git(self.wt_path, "status", "--porcelain")

        self._preview()

        self.assertEqual(_head(self.repo_dir), main_head)
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain"), main_status)
        self.assertEqual(_head(self.wt_path), wt_head)
        self.assertEqual(_git(self.wt_path, "status", "--porcelain"), wt_status)

    def test_preview_blocks_the_main_checkout(self) -> None:
        payload = self._preview(self.main_worktree_id)
        self.assertFalse(payload["can_merge"])
        self.assertEqual(payload["blockers"][0]["code"], "unmanaged_worktree")
        self.assertIn("主工作目录", payload["blockers"][0]["message"])

    def test_preview_blocks_an_unmanaged_worktree(self) -> None:
        ext_wt = self.root / "outside-wt"
        _git(self.repo_dir, "worktree", "add", "-b", "ext-branch", str(ext_wt))
        repos = svc.list_repositories(self.settings)
        ext = next(
            w for w in repos[0]["worktrees"] if w["path"] == str(ext_wt.resolve())
        )
        self.assertFalse(ext["managed"])
        payload = self._preview(str(ext["id"]))
        self.assertFalse(payload["can_merge"])
        self.assertEqual(payload["blockers"][0]["code"], "unmanaged_worktree")

    def test_preview_unknown_worktree_is_404(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._preview("sup-does-not-exist")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "worktree_not_found")

    def test_preview_blocks_a_busy_worktree(self) -> None:
        self._worktree_change_uncommitted()
        task = ags.create_task(
            self.settings,
            goal="还在跑的任务",
            repo_path=str(self.repo_dir),
            worktree_path=str(self.wt_path),
        )
        ags.transition(self.settings, str(task["id"]), "implementing")

        payload = self._preview()
        self.assertFalse(payload["can_merge"])
        self.assertEqual([b["code"] for b in payload["blockers"]], ["worktree_busy"])
        self.assertIn("任务", payload["blockers"][0]["message"])

    def test_preview_blocks_a_running_goal_but_allows_an_achieved_one(self) -> None:
        self._worktree_change_uncommitted()
        goal = goals.create_goal(
            None,
            self.settings,
            {
                "outcome": "整理计算器逻辑",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        self.assertEqual([b["code"] for b in self._preview()["blockers"]], ["worktree_busy"])

        goals._update_goal(self.settings, str(goal["id"]), state=goals.GOAL_ACHIEVED)
        payload = self._preview()
        self.assertTrue(payload["can_merge"], payload)
        self.assertEqual(payload["blockers"], [])

    def test_preview_blocks_when_main_checkout_is_on_another_branch(self) -> None:
        self._worktree_change_committed()
        _git(self.repo_dir, "checkout", "-b", "release")

        payload = self._preview()
        self.assertFalse(payload["can_merge"])
        self.assertEqual(payload["target_branch"], "main")
        self.assertEqual(
            [b["code"] for b in payload["blockers"]], ["target_not_checked_out"]
        )
        self.assertEqual(
            payload["blockers"][0]["message"],
            "主工作目录当前不在 main 分支上，请先切回去再合并",
        )

    def test_preview_blocks_when_main_checkout_is_dirty(self) -> None:
        self._worktree_change_committed()
        (self.repo_dir / "tracked.txt").write_text(
            "tracked baseline\nowner side, uncommitted\n", encoding="utf-8"
        )

        payload = self._preview()
        self.assertFalse(payload["can_merge"])
        self.assertEqual([b["code"] for b in payload["blockers"]], ["target_dirty"])
        self.assertEqual(
            payload["blockers"][0]["message"],
            "主工作目录里有还没提交的改动，为了不弄乱它，先不合并",
        )

    def test_preview_blocks_unsafe_repo_config_keys(self) -> None:
        self._worktree_change_committed()
        cases = (
            ("core.fsmonitor", "/bin/echo pwned"),
            ("core.hooksPath", "/tmp/evil-hooks"),
            ("core.sshCommand", "/bin/echo pwned"),
            ("core.pager", "/bin/echo pwned"),
            ("core.editor", "/bin/echo pwned"),
            ("sequence.editor", "/bin/echo pwned"),
            ("diff.external", "/bin/echo pwned"),
            ("credential.helper", "/bin/echo pwned"),
            ("gpg.program", "/bin/echo pwned"),
            ("filter.dirty.clean", "/bin/echo pwned"),
            ("filter.dirty.smudge", "/bin/echo pwned"),
            ("filter.dirty.process", "/bin/echo pwned"),
            ("merge.driver.driver", "/bin/echo pwned"),
            ("diff.binary.textconv", "/bin/echo pwned"),
            ("diff.binary.command", "/bin/echo pwned"),
            ("gpg.ssh.program", "/bin/echo pwned"),
        )
        try:
            for key, value in cases:
                self._set_config(key, value)
                codes = self._blocker_codes()
                self.assertIn("unsafe_repo_config", codes, key)
                # `git config --list` lowercases section/key names, so compare
                # case-insensitively while the message shows what git reported.
                messages = " ".join(b["message"] for b in self._preview()["blockers"]).lower()
                self.assertIn(key.lower(), messages, key)
                self._unset_config(key)
        finally:
            for key, _value in cases:
                subprocess.run(
                    ["git", "-C", str(self.repo_dir), "config", "--unset", key],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        # Every unsafe key is gone again: the merge is possible once more.
        self.assertTrue(self._preview()["can_merge"], self._preview())

    def test_empty_hookspath_and_disabled_fsmonitor_are_not_blockers(self) -> None:
        self._worktree_change_committed()
        self._set_config("core.hooksPath", "")
        self._set_config("core.fsmonitor", "false")
        self.assertTrue(self._preview()["can_merge"], self._preview())

    def test_preview_blocks_gitattributes_drivers(self) -> None:
        self._worktree_change_committed()
        for target in (self.wt_path, self.repo_dir):
            (target / ".gitattributes").write_text("*.txt filter=smash\n", encoding="utf-8")
            codes = self._blocker_codes()
            self.assertIn("unsafe_gitattributes", codes, target)
            (target / ".gitattributes").unlink()

        # A benign .gitattributes (and the unset form) names no driver.
        (self.repo_dir / ".gitattributes").write_text(
            "# comment\n*.txt text\n*.png -diff\n", encoding="utf-8"
        )
        self.assertTrue(self._preview()["can_merge"], self._preview())


class MergeExecutionTests(_MergeFixture):
    def test_happy_path_commits_then_merges(self) -> None:
        self._worktree_change_uncommitted()
        preview = self._preview()
        self.assertTrue(preview["can_merge"], preview)
        self.assertEqual(preview["commits_ahead"], 0)

        seen_argv: list[list[str]] = []
        real_run = merge_mod.subprocess.run

        def _recording_run(argv: Any, **kwargs: Any) -> Any:
            seen_argv.append([str(a) for a in argv])
            return real_run(argv, **kwargs)

        main_head_before = _head(self.repo_dir)
        with patch.object(merge_mod.subprocess, "run", _recording_run):
            result = self._merge({"confirm": True})

        self.assertEqual(result["merged"], True)
        self.assertEqual(result["target_branch"], "main")
        merge_commit = str(result["merge_commit"])
        self.assertEqual(merge_commit, _head(self.repo_dir))
        self.assertNotEqual(merge_commit, main_head_before)
        self.assertEqual(result["files_changed"], 2)

        # The merge commit is a real merge commit with two parents.
        parents = _git(self.repo_dir, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3, parents)
        self.assertEqual(parents[0], merge_commit)
        self.assertIn(_head(self.wt_path), parents[1:])
        self.assertEqual(
            _git(self.repo_dir, "log", "-1", "--pretty=%s").strip(),
            "合并 goal-calc（Conveyor）",
        )

        # The worktree's uncommitted work was committed as Conveyor, not the owner.
        worktree_log = _git(
            self.wt_path, "log", "-1", "--pretty=%an <%ae>|%cn <%ce>|%s"
        ).strip()
        self.assertEqual(
            worktree_log,
            "Conveyor <conveyor@localhost>|Conveyor <conveyor@localhost>|Conveyor：任务改动",
        )

        # The change really is in the main checkout, which stays clean.
        self.assertEqual(
            (self.repo_dir / "calc.py").read_text(encoding="utf-8"),
            "def add(a, b):\n    return a + b\n",
        )
        self.assertEqual(
            _git(self.repo_dir, "status", "--porcelain", "--untracked-files=no").strip(),
            "",
        )
        self.assertTrue((self.wt_path / "calc.py").exists())
        self.assertTrue(self._preview()["can_merge"])
        self.assertEqual(self._preview()["commits_ahead"], 0)

        # Never push anywhere.
        self.assertFalse(
            any("push" in part for argv in seen_argv for part in argv), seen_argv
        )

        # Audit row: through the helper and through an independent connection
        # to the very same database file the merge wrote to.
        audit = merge_mod.list_merge_audit(self.settings, worktree_id=self.worktree_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["worktree_id"], self.worktree_id)
        self.assertEqual(audit[0]["repo_id"], self.repo_id)
        self.assertEqual(audit[0]["source_branch"], "goal-calc")
        self.assertEqual(audit[0]["target_branch"], "main")
        self.assertEqual(audit[0]["merge_commit"], merge_commit)
        self.assertTrue(audit[0]["created_at"])

        db_file = svc._db_path(self.settings)
        self.assertTrue(db_file.exists(), db_file)
        raw = sqlite3.connect(str(db_file))
        try:
            raw.row_factory = sqlite3.Row
            rows = [dict(r) for r in raw.execute("SELECT * FROM supervisor_merges").fetchall()]
        finally:
            raw.close()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["merge_commit"], merge_commit)
        self.assertEqual(rows[0]["worktree_id"], self.worktree_id)
        self.assertEqual(rows[0]["source_branch"], "goal-calc")

    def test_merge_uses_goal_title_and_custom_message(self) -> None:
        goal = goals.create_goal(
            None,
            self.settings,
            {
                "outcome": "整理计算器逻辑",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        goals._update_goal(self.settings, str(goal["id"]), state=goals.GOAL_ACHIEVED)
        self._worktree_change_uncommitted()

        self._merge({"confirm": True})
        self.assertEqual(
            _git(self.wt_path, "log", "-1", "--pretty=%s").strip(), "Conveyor：整理计算器逻辑"
        )

        # A second round with an owner-supplied message.
        (self.wt_path / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
        self._merge({"confirm": True, "message": "第二轮：补一个边界用例"})
        self.assertEqual(
            _git(self.wt_path, "log", "-1", "--pretty=%s").strip(), "第二轮：补一个边界用例"
        )

    def test_merge_of_committed_work_only(self) -> None:
        self._worktree_change_committed()
        result = self._merge({"confirm": True})
        self.assertTrue(result["merged"])
        self.assertEqual(result["files_changed"], 2)
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")

    def test_hooks_are_never_executed(self) -> None:
        pre_marker = self.root / "pre-commit-ran"
        post_marker = self.root / "post-merge-ran"
        self._install_hook("pre-commit", pre_marker)
        self._install_hook("post-merge", post_marker)
        self._worktree_change_uncommitted()

        result = self._merge({"confirm": True})

        self.assertTrue(result["merged"])
        self.assertFalse(pre_marker.exists(), "pre-commit hook must not run")
        self.assertFalse(post_marker.exists(), "post-merge hook must not run")

    def test_conflict_is_aborted_and_leaves_the_main_checkout_untouched(self) -> None:
        self._worktree_change_committed("worktree side")
        (self.repo_dir / "tracked.txt").write_text(
            "tracked baseline\nowner side\n", encoding="utf-8"
        )
        _git(self.repo_dir, "add", "-A")
        _git(self.repo_dir, "commit", "-qm", "owner side")
        main_head_before = _head(self.repo_dir)

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})

        exc = ctx.exception
        self.assertEqual(exc.status, 409)
        self.assertEqual(exc.to_payload().get("code"), "merge_conflict")
        self.assertEqual(
            exc.to_payload().get("error"),
            "合并有冲突，已经撤销，主工作目录没有变化。可以让执行者在工作目录里先合并主分支再试。",
        )
        self.assertEqual(exc.to_payload().get("conflicts"), ["tracked.txt"])
        self.assertEqual(_head(self.repo_dir), main_head_before)
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")
        self.assertEqual(_git(self.repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertFalse((self.repo_dir / "MERGE_HEAD").exists())
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])

    def test_nothing_to_merge(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "nothing_to_merge")

    def test_nothing_to_merge_after_an_earlier_merge(self) -> None:
        self._worktree_change_committed()
        self._merge({"confirm": True})
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "nothing_to_merge")

    def test_merge_blocked_by_busy_worktree(self) -> None:
        self._worktree_change_uncommitted()
        task = ags.create_task(
            self.settings,
            goal="还在跑的任务",
            repo_path=str(self.repo_dir),
            worktree_path=str(self.wt_path),
        )
        ags.transition(self.settings, str(task["id"]), "queued")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "worktree_busy")
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")

    def test_merge_blocked_when_main_is_on_another_branch(self) -> None:
        self._worktree_change_committed()
        _git(self.repo_dir, "checkout", "-b", "release")
        wt_head_before = _head(self.wt_path)
        main_head_before = _head(self.repo_dir)
        wt_status_before = _git(self.wt_path, "status", "--porcelain")
        main_status_before = _git(self.repo_dir, "status", "--porcelain")

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "target_not_checked_out")

        # A refused merge changes nothing anywhere: no commit in the worktree,
        # nothing merged into the main checkout, no audit row.
        self.assertEqual(_head(self.wt_path), wt_head_before)
        self.assertEqual(_git(self.wt_path, "status", "--porcelain"), wt_status_before)
        self.assertEqual(_head(self.repo_dir), main_head_before)
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain"), main_status_before)
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])

    def test_merge_makes_no_change_when_a_blocker_applies(self) -> None:
        """The blocker gate runs before step (a): uncommitted work stays uncommitted."""
        self._worktree_change_uncommitted()
        _git(self.repo_dir, "checkout", "-b", "release")
        wt_head_before = _head(self.wt_path)
        wt_status_before = _git(self.wt_path, "status", "--porcelain")

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True, "message": "不该被提交"})
        self.assertEqual(ctx.exception.to_payload().get("code"), "target_not_checked_out")

        self.assertEqual(_head(self.wt_path), wt_head_before)
        self.assertEqual(_git(self.wt_path, "status", "--porcelain"), wt_status_before)
        self.assertNotIn("不该被提交", _git(self.wt_path, "log", "--pretty=%s"))
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])

    def test_merge_blocked_when_main_is_dirty(self) -> None:
        self._worktree_change_committed()
        (self.repo_dir / "tracked.txt").write_text("dirty owner edit\n", encoding="utf-8")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "target_dirty")

    def test_merge_blocked_by_unsafe_config_and_gitattributes(self) -> None:
        self._worktree_change_committed()
        self._set_config("filter.dirty.smudge", "/bin/echo pwned")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_repo_config")
        self._unset_config("filter.dirty.smudge")

        (self.wt_path / ".gitattributes").write_text("*.txt merge=keep\n", encoding="utf-8")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_gitattributes")
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")

    def test_merge_refuses_the_main_checkout(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True}, worktree_id=self.main_worktree_id)
        self.assertEqual(ctx.exception.to_payload().get("code"), "unmanaged_worktree")

    def test_merge_releases_the_worktree_runtime_lock(self) -> None:
        self._worktree_change_committed()
        self._merge({"confirm": True})
        conn = ags._connect(self.settings)
        try:
            rows = conn.execute(
                "SELECT task_id FROM supervisor_locks WHERE worktree_path = ?",
                (str(self.wt_path),),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([str(r["task_id"]) for r in rows], [])

    def test_merge_holds_the_worktree_runtime_lock_while_merging(self) -> None:
        """No task may be dispatched on the worktree while the merge runs."""
        self._worktree_change_committed()
        holders: list[list[str]] = []
        real_run_merge = merge_mod._run_merge

        def _spy(settings: Any, scope: dict[str, Any], message: str) -> dict[str, Any]:
            conn = ags._connect(settings)
            try:
                rows = conn.execute(
                    "SELECT task_id FROM supervisor_locks WHERE worktree_path = ?",
                    (str(scope["worktree_path"]),),
                ).fetchall()
            finally:
                conn.close()
            holders.append([str(r["task_id"]) for r in rows])
            return real_run_merge(settings, scope, message)

        with patch.object(merge_mod, "_run_merge", _spy):
            self._merge({"confirm": True})

        self.assertEqual(holders, [[f"merge-{self.worktree_id}"]])

    def test_repo_locks_are_per_repository(self) -> None:
        first = merge_mod._repo_merge_lock("repo-a")
        self.assertIs(first, merge_mod._repo_merge_lock("repo-a"))
        self.assertIsNot(first, merge_mod._repo_merge_lock("repo-b"))


class MergeBodyValidationTests(_MergeFixture):
    def test_confirm_is_required(self) -> None:
        for body in ({}, {"confirm": False}, {"confirm": "true"}, {"message": "x"}):
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                self._merge(body)
            self.assertEqual(ctx.exception.status, 400, body)
            self.assertEqual(ctx.exception.to_payload().get("code"), "confirm_required")

    def test_message_must_be_a_short_string(self) -> None:
        self._worktree_change_uncommitted()
        for body in ({"confirm": True, "message": 5}, {"confirm": True, "message": "x" * 201}):
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                self._merge(body)
            self.assertEqual(ctx.exception.status, 400, body)
            self.assertEqual(ctx.exception.to_payload().get("code"), "invalid_message")

    def test_non_object_body_is_400(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge(["confirm"])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.to_payload().get("code"), "invalid_body")


class MergeGitHardeningTests(_MergeFixture):
    def test_git_argv_forces_hooks_fsmonitor_and_editor_off(self) -> None:
        argv = merge_mod._git_argv(str(self.wt_path), ["status", "--porcelain"])
        self.assertEqual(argv[0], "git")
        self.assertEqual(argv[1], "--no-pager")
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("core.fsmonitor=false", argv)
        self.assertIn("commit.gpgsign=false", argv)
        self.assertIn("tag.gpgsign=false", argv)
        self.assertIn("core.editor=true", argv)
        self.assertEqual(argv[-2:], ["status", "--porcelain"])

    def test_git_env_strips_git_dir_and_optional_locks(self) -> None:
        env = merge_mod._git_env(self.settings)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_EDITOR"], "true")

    def test_conveyor_identity_leads_the_commit_subcommand(self) -> None:
        """`git commit -c user.name=…` would be read as `--reedit-message=…`."""
        argv = merge_mod._git_argv(
            str(self.wt_path),
            ["commit", "--no-verify", "-m", "x"],
            config=("user.name=Conveyor", "user.email=conveyor@localhost"),
        )
        # git -c options must precede the subcommand.
        self.assertLess(argv.index("user.name=Conveyor"), argv.index("commit"))
        self.assertNotIn("-c", argv[argv.index("commit"):])

    def test_config_key_classification(self) -> None:
        for key in (
            "core.fsmonitor", "core.hooksPath", "core.sshCommand", "core.pager",
            "core.editor", "sequence.editor", "diff.external", "credential.helper",
            "gpg.program", "gpg.ssh.program", "filter.x.clean", "filter.x.smudge",
            "filter.x.process", "merge.x.driver", "diff.x.textconv", "diff.x.command",
        ):
            self.assertTrue(merge_mod._config_key_runs_commands(key), key)
        for key in (
            "user.name", "core.autocrlf", "remote.origin.url",
            "diff.noprefix", "filter.x.required", "merge.log", "core.repositoryformatversion",
        ):
            self.assertFalse(merge_mod._config_key_runs_commands(key), key)

    def test_unsafe_values_must_actually_name_a_program(self) -> None:
        self.assertFalse(merge_mod._value_runs_commands(""))
        self.assertFalse(merge_mod._value_runs_commands("false"))
        self.assertTrue(merge_mod._value_runs_commands("/bin/echo pwned"))

    def test_branch_names_must_not_look_like_git_options(self) -> None:
        for branch in ("goal-calc", "main", "release/1.0", "sup/ab12cd34"):
            self.assertTrue(merge_mod._is_safe_branch_name(branch), branch)
        for branch in ("", "-weird", "a..b", "has space", "tilde~1"):
            self.assertFalse(merge_mod._is_safe_branch_name(branch), branch)


class MergeHttpTests(_MergeFixture):
    def _get(self, path: str, settings: Any = None) -> tuple[int, dict[str, Any]]:
        from web_supervisor_api import handle_get

        handler = _FakeHandler(settings or self.settings)
        handle_get(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        return handler.response

    def _post(self, path: str, body: dict[str, Any], settings: Any = None) -> tuple[int, dict[str, Any]]:
        from web_supervisor_api import handle_post

        handler = _FakeHandler(settings or self.settings)
        handle_post(handler, path, path.strip("/").split("/"), body)
        assert handler.response is not None
        return handler.response

    def test_merge_preview_route(self) -> None:
        self._worktree_change_uncommitted()
        code, body = self._get(f"/api/supervisor/worktrees/{self.worktree_id}/merge-preview")

        self.assertEqual(code, 200, body)
        self.assertEqual(body["worktree_id"], self.worktree_id)
        self.assertEqual(body["source_branch"], "goal-calc")
        self.assertEqual(body["target_branch"], "main")
        self.assertTrue(body["can_merge"])
        self.assertEqual(body["blockers"], [])
        self.assertTrue(body["has_uncommitted"])
        self.assertIn("calc.py", {entry["path"] for entry in body["files"]})

    def test_merge_preview_route_unknown_worktree(self) -> None:
        code, body = self._get("/api/supervisor/worktrees/wt-nope/merge-preview")
        self.assertEqual(code, 404)
        self.assertEqual(body.get("code"), "worktree_not_found")

    def test_merge_route(self) -> None:
        self._worktree_change_uncommitted()
        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge", {"confirm": True}
        )

        self.assertEqual(code, 200, body)
        self.assertTrue(body["merged"])
        self.assertEqual(body["target_branch"], "main")
        self.assertEqual(body["merge_commit"], _head(self.repo_dir))
        self.assertEqual(body["files_changed"], 2)

    def test_merge_route_requires_confirm(self) -> None:
        self._worktree_change_uncommitted()
        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge", {"message": "合并吧"}
        )
        self.assertEqual(code, 400)
        self.assertEqual(body.get("code"), "confirm_required")
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")

    def test_merge_route_conflict_payload(self) -> None:
        self._worktree_change_committed("worktree side")
        (self.repo_dir / "tracked.txt").write_text("owner side\n", encoding="utf-8")
        _git(self.repo_dir, "add", "-A")
        _git(self.repo_dir, "commit", "-qm", "owner side")

        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge", {"confirm": True}
        )
        self.assertEqual(code, 409)
        self.assertEqual(body.get("code"), "merge_conflict")
        self.assertEqual(body.get("conflicts"), ["tracked.txt"])
        self.assertIn("已经撤销", body.get("error", ""))

    def test_merge_routes_require_feature_enabled(self) -> None:
        off_settings = dataclasses.replace(
            _make_settings(self.root / "off"),
            agent_supervisor_enabled=False,
        )
        code, body = self._get(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge-preview", off_settings
        )
        self.assertEqual(code, 409)
        self.assertEqual(body.get("code"), "agent_supervisor_disabled")

        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge",
            {"confirm": True},
            off_settings,
        )
        self.assertEqual(code, 409)
        self.assertEqual(body.get("code"), "agent_supervisor_disabled")


class MergeRepoExecRiskTests(_MergeFixture):
    """Agent-planted config/attributes never run during preview or merge."""

    def setUp(self) -> None:
        super().setUp()
        self.marker = self.root / "filter-ran"
        self.filter_cmd = f"touch {shlex.quote(str(self.marker))}; cat"

    def _common_dir(self) -> Path:
        raw = _git(self.wt_path, "rev-parse", "--git-common-dir").strip()
        path = Path(raw)
        return path if path.is_absolute() else (self.wt_path / path).resolve()

    def _plant_subdir_filter(self) -> None:
        """Tracked sub/data.txt with sub/.gitattributes → `pwn`, plus the filter in .git/config."""
        (self.wt_path / "sub").mkdir()
        (self.wt_path / "sub" / "data.txt").write_text("one\n", encoding="utf-8")
        (self.wt_path / "sub" / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
        _git(self.wt_path, "add", "-A")
        _git(self.wt_path, "commit", "-qm", "sub dir with attributes")
        (self.wt_path / "sub" / "data.txt").write_text("one\ntwo\n", encoding="utf-8")
        self._set_config("filter.pwn.clean", self.filter_cmd)

    def _forbid_content_reads(self):
        """Patches that fail the test if anything that reads worktree content runs."""

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("must not read worktree content while the repo is unsafe")

        return (
            patch.object(merge_mod, "_is_clean", _boom),
            patch.object(merge_mod, "_has_uncommitted", _boom),
            patch.object(merge_mod.diff_mod, "build_worktree_diff", _boom),
            patch.object(merge_mod, "_commit_worktree_changes", _boom),
        )

    def _run_with_content_reads_forbidden(self, fn: Any) -> Any:
        patches = self._forbid_content_reads()
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_preview_with_filter_and_subdir_attributes_runs_nothing(self) -> None:
        self._plant_subdir_filter()
        payload = self._run_with_content_reads_forbidden(self._preview)

        self.assertFalse(payload["can_merge"])
        codes = [b["code"] for b in payload["blockers"]]
        self.assertIn("unsafe_repo_config", codes)
        self.assertIn("unsafe_gitattributes", codes)
        messages = " ".join(b["message"] for b in payload["blockers"])
        self.assertIn("filter.pwn.clean", messages)
        self.assertIn("sub/.gitattributes", messages)
        self.assertEqual(payload["files"], [])
        self.assertFalse(payload["has_uncommitted"])
        self.assertFalse(self.marker.exists(), "the planted filter must never run")

        # Same through the real (unpatched) preview: still nothing runs.
        self._preview()
        self.assertFalse(self.marker.exists())

    def test_merge_with_filter_is_refused_before_any_commit(self) -> None:
        self._plant_subdir_filter()
        wt_head = _head(self.wt_path)
        main_head = _head(self.repo_dir)

        def _attempt() -> None:
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                self._merge({"confirm": True})
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_repo_config")

        self._run_with_content_reads_forbidden(_attempt)
        _attempt()  # and once more without the patches
        self.assertFalse(self.marker.exists())
        self.assertEqual(_head(self.wt_path), wt_head)
        self.assertEqual(_head(self.repo_dir), main_head)
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])

    def test_filter_defined_through_include_path_is_blocked(self) -> None:
        self._worktree_change_committed()
        included = self.root / "included.cfg"
        included.write_text(f'[filter "pwn"]\n\tclean = {self.filter_cmd}\n', encoding="utf-8")
        self._set_config("include.path", str(included))
        (self.wt_path / ".gitattributes").write_text("*.py filter=pwn\n", encoding="utf-8")

        payload = self._run_with_content_reads_forbidden(self._preview)
        self.assertFalse(payload["can_merge"])
        messages = " ".join(b["message"] for b in payload["blockers"]).lower()
        self.assertIn("include.path", messages)
        self.assertIn("filter.pwn.clean", messages)
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_repo_config")
        self.assertFalse(self.marker.exists())

    def test_driver_in_info_attributes_is_blocked(self) -> None:
        self._worktree_change_committed()
        info = self._common_dir() / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("*.txt merge=evil\n", encoding="utf-8")

        payload = self._run_with_content_reads_forbidden(self._preview)
        self.assertEqual(
            [b["code"] for b in payload["blockers"]], ["unsafe_gitattributes"]
        )
        self.assertIn("info/attributes", payload["blockers"][0]["message"])
        main_head = _head(self.repo_dir)
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_gitattributes")
        self.assertEqual(_head(self.repo_dir), main_head)

    def test_subdir_gitattributes_in_main_checkout_is_blocked(self) -> None:
        self._worktree_change_committed()
        (self.repo_dir / "docs").mkdir()
        (self.repo_dir / "docs" / ".gitattributes").write_text("*.md filter=x\n", encoding="utf-8")
        _git(self.repo_dir, "add", "-A")
        _git(self.repo_dir, "commit", "-qm", "attributes in a subdir of main")
        codes = self._blocker_codes()
        self.assertIn("unsafe_gitattributes", codes)

    def test_busy_worktree_preview_still_never_reads_content_when_unsafe(self) -> None:
        task = ags.create_task(
            self.settings,
            goal="还在跑的任务",
            repo_path=str(self.repo_dir),
            worktree_path=str(self.wt_path),
        )
        ags.transition(self.settings, str(task["id"]), "implementing")
        self._plant_subdir_filter()
        payload = self._run_with_content_reads_forbidden(self._preview)
        self.assertFalse(payload["can_merge"])
        self.assertEqual(payload["files"], [])
        self.assertFalse(self.marker.exists())


class MergeBranchChangedTests(_MergeFixture):
    """The worktree must still be on the branch recorded when it was created."""

    def _recorded_branch(self) -> str | None:
        conn = svc._connect(self.settings)
        try:
            row = conn.execute(
                "SELECT branch FROM supervisor_worktrees WHERE id = ?", (self.worktree_id,)
            ).fetchone()
        finally:
            conn.close()
        return row["branch"]

    def test_branch_is_recorded_at_creation(self) -> None:
        self.assertEqual(self._recorded_branch(), "goal-calc")

    def test_worktree_head_switched_to_main_is_refused(self) -> None:
        self._worktree_change_uncommitted()
        main_ref_before = _git(self.repo_dir, "rev-parse", "refs/heads/main").strip()
        # The agent rewrites .git/worktrees/<n>/HEAD to point at main.
        _git(self.wt_path, "symbolic-ref", "HEAD", "refs/heads/main")

        preview = self._preview()
        self.assertFalse(preview["can_merge"])
        self.assertEqual([b["code"] for b in preview["blockers"]], ["branch_changed"])
        self.assertEqual(
            preview["blockers"][0]["message"], "工作目录的分支被改动过，为安全起见不合并"
        )

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True, "message": "不该提交到 main"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "branch_changed")

        self.assertEqual(
            _git(self.repo_dir, "rev-parse", "refs/heads/main").strip(), main_ref_before
        )
        self.assertNotIn("不该提交到 main", _git(self.repo_dir, "log", "--all", "--pretty=%s"))
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])

    def test_worktree_head_switched_to_another_branch_is_refused(self) -> None:
        self._worktree_change_committed()
        _git(self.wt_path, "checkout", "-q", "-b", "sneaky")
        self.assertEqual(self._blocker_codes(), ["branch_changed"])
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "branch_changed")

    def test_branch_ref_made_symbolic_to_main_is_refused(self) -> None:
        self._worktree_change_uncommitted()
        main_ref_before = _git(self.repo_dir, "rev-parse", "refs/heads/main").strip()
        _git(self.repo_dir, "symbolic-ref", "refs/heads/goal-calc", "refs/heads/main")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._merge({"confirm": True})
        self.assertEqual(ctx.exception.to_payload().get("code"), "branch_changed")
        self.assertEqual(
            _git(self.repo_dir, "rev-parse", "refs/heads/main").strip(), main_ref_before
        )

    def test_switching_back_restores_the_merge(self) -> None:
        self._worktree_change_committed()
        _git(self.wt_path, "checkout", "-q", "-b", "sneaky")
        self.assertEqual(self._blocker_codes(), ["branch_changed"])
        _git(self.wt_path, "checkout", "-q", "goal-calc")
        result = self._merge({"confirm": True})
        self.assertTrue(result["merged"])

    def test_legacy_row_without_recorded_branch(self) -> None:
        """Rows from older builds: only Conveyor's own auto-generated names are accepted."""
        repo = svc.list_repositories(self.settings)[0]
        auto = svc.create_managed_worktree(self.settings, repo["id"])
        auto_path = Path(str(auto["path"]))
        conn = svc._connect(self.settings)
        try:
            conn.execute(
                "UPDATE supervisor_worktrees SET branch = NULL WHERE id IN (?, ?)",
                (str(auto["id"]), self.worktree_id),
            )
        finally:
            conn.close()

        (auto_path / "auto.txt").write_text("auto\n", encoding="utf-8")
        _git(auto_path, "add", "-A")
        _git(auto_path, "commit", "-qm", "auto work")
        self.assertTrue(self._preview(str(auto["id"]))["can_merge"])

        # A custom branch name cannot be verified without a record.
        self._worktree_change_committed()
        self.assertEqual(self._blocker_codes(), ["branch_changed"])

    def test_tag_named_like_the_branch_is_not_merged(self) -> None:
        self._worktree_change_committed()
        reviewed = _head(self.wt_path)
        # A tag with the branch's name pointing at different content.
        _git(self.repo_dir, "tag", "goal-calc", "HEAD")
        result = self._merge({"confirm": True})
        parents = _git(self.repo_dir, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(parents[0], result["merge_commit"])
        self.assertIn(reviewed, parents[1:])

    def test_schema_migration_adds_branch_column(self) -> None:
        db_file = svc._db_path(self.settings)
        raw = sqlite3.connect(str(db_file))
        try:
            cols = [r[1] for r in raw.execute("PRAGMA table_info(supervisor_worktrees)")]
        finally:
            raw.close()
        self.assertIn("branch", cols)

        # An old database without the column gets it on first connect.
        legacy_root = self.root / "legacy"
        legacy_settings = _make_settings(legacy_root)
        legacy_db = svc._db_path(legacy_settings)
        old = sqlite3.connect(str(legacy_db))
        try:
            old.executescript(
                """CREATE TABLE supervisor_worktrees (
                    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
                    canonical_path TEXT NOT NULL UNIQUE, managed INTEGER NOT NULL DEFAULT 0,
                    chat_session_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)"""
            )
        finally:
            old.close()
        svc._INITIALIZED_DBS.discard(str(legacy_db))
        conn = svc._connect(legacy_settings)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(supervisor_worktrees)")]
        finally:
            conn.close()
        self.assertIn("branch", cols)


if __name__ == "__main__":
    unittest.main()
