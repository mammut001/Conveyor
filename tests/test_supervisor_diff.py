"""tests/test_supervisor_diff.py — read-only diff endpoints for Supervisor tasks and goals.

Uses a disposable state root, a real disposable git repository, and a real
managed worktree created through `supervisor_service.create_managed_worktree`
(the same pattern as `tests/test_supervisor_validation.py` /
`tests/test_supervisor_goals.py`). No real model API or CLI is ever called.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
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

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_diff as diff_mod
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
from conveyor.core.config import Settings

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
        conveyor_web_token="diff-test-token-0123456789-abcdefghijklmnop",
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
    (repo_dir / "README.md").write_text("# Disposable Diff Repo\n", encoding="utf-8")
    (repo_dir / "doomed.txt").write_text("this file is going away\n", encoding="utf-8")
    (repo_dir / "tracked.txt").write_text("tracked baseline\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "initial commit")
    return _head(repo_dir)


class FakeChatExecutor:
    """Stand-in for every supervisor executor: always available, scripted output."""

    name = "fake"

    def __init__(self, settings: Any, responder: Any) -> None:
        self.settings = settings
        self._responder = responder
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def run(self, *, role: Any, prompt: str, worktree_path: str,
                  timeout_seconds: int, task_id: str = "",
                  context: Any = None) -> ags.ExecutionResult:
        self.calls += 1
        await asyncio.sleep(0.01)
        return ags.ExecutionResult(
            success=True,
            output=self._responder(prompt),
            started_at=ags._utc_now(),
            finished_at=ags._utc_now(),
        )

    async def cancel(self, execution_id: str) -> bool:
        return False


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


class _DiffFixture(unittest.TestCase):
    """Shared setup: one real repo + one real managed worktree."""

    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-diff-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/diff")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = Path(str(wt["path"]))
        self.managed_root = Path(
            svc._canonical_internal_path(str(svc._managed_worktree_root(self.settings)))
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _create_task(self, *, worktree_path: Any = None, base_sha: str = "",
                     metadata: dict[str, Any] | None = None) -> str:
        task = ags.create_task(
            self.settings,
            goal="diff fixture",
            repo_path=str(self.repo_dir),
            worktree_path=str(worktree_path if worktree_path is not None else self.wt_path),
            base_sha=base_sha,
            metadata=metadata,
        )
        return str(task["id"])

    def _make_candidate_changes(self) -> str:
        """Modified (unstaged + committed) + staged add + deleted + untracked files."""
        # unstaged modification of a tracked file
        (self.wt_path / "README.md").write_text(
            "# Disposable Diff Repo (edited)\n", encoding="utf-8"
        )
        # staged addition
        (self.wt_path / "added.txt").write_text("first line\nsecond line\n", encoding="utf-8")
        _git(self.wt_path, "add", "added.txt")
        # deleted tracked file
        (self.wt_path / "doomed.txt").unlink()
        # committed change: must still show up against the captured base
        (self.wt_path / "tracked.txt").write_text(
            "tracked baseline\ntracked candidate commit\n", encoding="utf-8"
        )
        _git(self.wt_path, "add", "tracked.txt")
        _git(self.wt_path, "commit", "-qm", "candidate commit")
        # untracked text (inlined), untracked binary, untracked oversize text
        (self.wt_path / "notes.txt").write_text("note line one\nnote line two\n", encoding="utf-8")
        (self.wt_path / "blob.bin").write_bytes(b"\x00\x01\x02BINARYNOTDIFFABLE\x00")
        (self.wt_path / "big.txt").write_text("x" * 70_000 + "\n", encoding="utf-8")
        return _head(self.wt_path)

    def _by_path(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {entry["path"]: entry for entry in payload["files"]}


class TaskDiffPayloadTests(_DiffFixture):
    def test_task_diff_reports_committed_staged_unstaged_and_untracked(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        head_after = self._make_candidate_changes()

        payload = svc.get_task_diff(self.settings, task_id)

        self.assertEqual(
            set(payload),
            {"task_id", "base_sha", "head_sha", "files", "diff", "truncated", "untracked"},
        )
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["base_sha"], base)
        self.assertEqual(payload["head_sha"], head_after)
        self.assertFalse(payload["truncated"])

        files = self._by_path(payload)
        self.assertEqual(files["README.md"]["status"], "M")
        self.assertGreaterEqual(files["README.md"]["additions"], 1)
        self.assertGreaterEqual(files["README.md"]["deletions"], 1)
        self.assertEqual(files["added.txt"]["status"], "A")
        self.assertEqual(files["added.txt"]["additions"], 2)
        self.assertEqual(files["added.txt"]["deletions"], 0)
        self.assertEqual(files["doomed.txt"]["status"], "D")
        self.assertEqual(files["tracked.txt"]["status"], "M")
        self.assertEqual(files["notes.txt"]["status"], "?")
        self.assertEqual(files["notes.txt"]["additions"], 2)
        self.assertEqual(files["blob.bin"]["status"], "?")
        self.assertEqual(files["big.txt"]["status"], "?")
        self.assertEqual(files["big.txt"]["additions"], 0)

        for name in ("notes.txt", "blob.bin", "big.txt"):
            self.assertIn(name, payload["untracked"])

        diff = payload["diff"]
        self.assertIn("diff --git a/README.md b/README.md", diff)
        self.assertIn("+tracked candidate commit", diff)
        self.assertIn("deleted file mode", diff)
        self.assertIn("+++ /dev/null", diff)
        self.assertIn("new file mode", diff)
        self.assertIn("+note line one", diff)
        # binary and oversize untracked files are listed only, never inlined
        self.assertNotIn("BINARYNOTDIFFABLE", diff)
        self.assertNotIn("big.txt", diff)

    def test_task_diff_rename_is_reported(self) -> None:
        (self.wt_path / "before-rename.txt").write_text("rename me\n", encoding="utf-8")
        _git(self.wt_path, "add", "before-rename.txt")
        _git(self.wt_path, "commit", "-qm", "add file to rename")
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)

        _git(self.wt_path, "mv", "before-rename.txt", "renamed.txt")

        payload = svc.get_task_diff(self.settings, task_id)
        files = self._by_path(payload)
        self.assertIn("renamed.txt", files)
        self.assertNotIn("before-rename.txt", files)
        # "R" whenever git rename detection is on (the default); A/D otherwise.
        self.assertIn(files["renamed.txt"]["status"], ("R", "A"), files["renamed.txt"])
        if files["renamed.txt"]["status"] == "R":
            self.assertIn("rename from", payload["diff"])
            self.assertIn("rename to", payload["diff"])

    def test_task_diff_caps_at_200k_chars(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        (self.wt_path / "huge.txt").write_text(("xxxxxx\n" * 40_000), encoding="utf-8")
        _git(self.wt_path, "add", "huge.txt")

        payload = svc.get_task_diff(self.settings, task_id)

        self.assertTrue(payload["truncated"])
        self.assertLessEqual(len(payload["diff"]), diff_mod.MAX_DIFF_CHARS)
        self.assertLess(len(payload["diff"]), 280_000)
        self.assertEqual(self._by_path(payload)["huge.txt"]["additions"], 40_000)

    def test_invalid_base_sha_falls_back_to_queued_target_sha(self) -> None:
        base = _head(self.wt_path)
        missing = "0" * 40
        task_id = self._create_task(
            base_sha=missing,
            metadata={"queued_target_sha": base},
        )
        (self.wt_path / "added.txt").write_text("fallback base\n", encoding="utf-8")

        payload = svc.get_task_diff(self.settings, task_id)

        self.assertEqual(payload["base_sha"], base)
        self.assertNotEqual(payload["base_sha"], missing)
        self.assertIn("+fallback base", payload["diff"])

    def test_missing_base_sha_falls_back_to_head(self) -> None:
        task_id = self._create_task()
        (self.wt_path / "added.txt").write_text("head base\n", encoding="utf-8")

        payload = svc.get_task_diff(self.settings, task_id)

        self.assertEqual(payload["base_sha"], _head(self.wt_path))
        self.assertIn("+head base", payload["diff"])

    def test_missing_worktree_is_404(self) -> None:
        vanished = self.managed_root / "sup-vanished"
        self.assertFalse(vanished.exists())
        task_id = self._create_task(worktree_path=vanished)

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.get_task_diff(self.settings, task_id)

        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "worktree_missing")
        self.assertTrue(ctx.exception.to_payload().get("error"))

    def test_worktree_outside_managed_root_is_refused(self) -> None:
        # The main repository root is a real git repo, but not a managed worktree.
        outside = self.root / "outside"
        outside.mkdir()
        _git(outside, "init", "-b", "main")
        _git(outside, "config", "user.email", "test@example.com")
        _git(outside, "config", "user.name", "Supervisor Test")
        (outside / "keep.txt").write_text("outside\n", encoding="utf-8")
        _git(outside, "add", "-A")
        _git(outside, "commit", "-qm", "outside repo")

        for raw_path in (self.repo_dir, outside):
            task_id = self._create_task(worktree_path=raw_path)
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                svc.get_task_diff(self.settings, task_id)
            self.assertEqual(ctx.exception.status, 403)
            self.assertEqual(
                ctx.exception.to_payload().get("code"), "worktree_outside_managed_root"
            )

    def test_unknown_task_is_404(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.get_task_diff(self.settings, "sup-does-not-exist")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "task_not_found")

    def test_git_argv_disables_fsmonitor_and_pager(self) -> None:
        """An agent can write `core.fsmonitor=<command>` into the shared repo config;
        git would then run it on every diff/ls-files the owner triggers by viewing
        changes, so every invocation must force it off."""
        argv = diff_mod._git_argv(str(self.wt_path), ["diff", "HEAD"])
        self.assertEqual(argv[0], "git")
        self.assertIn("--no-pager", argv)
        self.assertIn("-c", argv)
        self.assertIn("core.fsmonitor=false", argv)
        self.assertIn("core.quotePath=false", argv)
        self.assertEqual(argv[-2:], ["diff", "HEAD"])

    def test_diff_calls_disable_external_diff_drivers(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        seen: list[list[str]] = []
        real_git = diff_mod._git

        def _recording_git(settings: Any, worktree: str, args: list[str], **kwargs: Any):
            seen.append(list(args))
            return real_git(settings, worktree, args, **kwargs)

        with patch.object(diff_mod, "_git", _recording_git):
            svc.get_task_diff(self.settings, task_id)

        diff_calls = [args for args in seen if args[:1] == ["diff"]]
        self.assertEqual(len(diff_calls), 3, seen)
        for args in diff_calls:
            self.assertIn("--no-ext-diff", args)
        self.assertTrue(any("--name-status" in args for args in diff_calls), seen)
        self.assertTrue(any("--numstat" in args for args in diff_calls), seen)
        self.assertTrue(any("--no-color" in args for args in diff_calls), seen)


class GoalDiffTests(_DiffFixture):
    def setUp(self) -> None:
        super().setUp()
        self.responder = self._continue_responder
        self.fake = FakeChatExecutor(self.settings, lambda prompt: self.responder(prompt))
        self._executor_patch = patch.object(ags, "get_executor", lambda _s, _n: self.fake)
        self._executor_patch.start()
        self.addCleanup(self._executor_patch.stop)

        # A finished goal queues an owner notification whose daemon worker then
        # writes its dedupe row into the goals DB under this temp state dir
        # after the test body ended, racing tearDown's rmtree. None of these
        # tests assert on notifications, so the call is a no-op here. Patch the
        # name supervisor_goals calls (it imported the function directly, so
        # patching only goal_notify.notify_goal_change would not stick) plus
        # the goal_notify attribute for any late-bound caller.
        for module in (goals, goal_notify):
            notify_patch = patch.object(module, "notify_goal_change", lambda *_a, **_kw: None)
            notify_patch.start()
            self.addCleanup(notify_patch.stop)

    def _continue_responder(self, _prompt: str) -> str:
        return json.dumps(
            {
                "decision": "continue",
                "reason": "还需要第二轮",
                "next_prompt": "继续补充改动",
                "question": "",
                "summary": "第一轮结束。",
            },
            ensure_ascii=False,
        )

    def _advance(self) -> None:
        asyncio.run(goals.advance_goals(None, self.settings))

    def _create_goal(self) -> dict[str, Any]:
        return goals.create_goal(
            None,
            self.settings,
            {"outcome": "在 managed worktree 里持续改动", "repo_id": self.repo_id,
             "worktree_id": self.worktree_id},
        )

    def test_goal_diff_spans_both_iteration_tasks(self) -> None:
        goal = self._create_goal()
        self._advance()
        iterations = goals.get_goal(self.settings, goal["id"])["iterations"]
        self.assertEqual(len(iterations), 1)
        first_task_id = str(iterations[0]["task_id"])
        first_base = str(ags.get_task(self.settings, first_task_id)["base_sha"])
        self.assertTrue(first_base)

        # Iteration 1 candidate work: a committed change on the goal's worktree.
        (self.wt_path / "tracked.txt").write_text(
            "tracked baseline\niteration-one-commit\n", encoding="utf-8"
        )
        _git(self.wt_path, "add", "tracked.txt")
        _git(self.wt_path, "commit", "-qm", "iteration one")

        ags.transition(self.settings, first_task_id, "failed", stage="failed")
        self._advance()

        detail = goals.get_goal(self.settings, goal["id"])
        self.assertEqual(len(detail["iterations"]), 2)
        second_task_id = str(detail["iterations"][1]["task_id"])
        second_base = str(ags.get_task(self.settings, second_task_id)["base_sha"])
        self.assertNotEqual(second_base, first_base)

        # Iteration 2 candidate work: an untracked file.
        (self.wt_path / "iteration-two.txt").write_text("iteration two\n", encoding="utf-8")

        goal_payload = goals.get_goal_diff(self.settings, goal["id"])
        self.assertEqual(set(goal_payload) - {"goal_id"},
                         {"base_sha", "head_sha", "files", "diff", "truncated", "untracked"})
        self.assertEqual(goal_payload["goal_id"], goal["id"])
        # Whole-goal view: anchored on the FIRST task's base, so both iterations show.
        self.assertEqual(goal_payload["base_sha"], first_base)
        self.assertIn("+iteration-one-commit", goal_payload["diff"])
        self.assertIn("iteration-two.txt", goal_payload["untracked"])
        self.assertIn("+iteration two", goal_payload["diff"])

        # A single task only sees its own slice: iteration 1's commit is already base.
        task_payload = svc.get_task_diff(self.settings, second_task_id)
        self.assertEqual(task_payload["base_sha"], second_base)
        self.assertNotIn("+iteration-one-commit", task_payload["diff"])
        self.assertIn("iteration-two.txt", task_payload["untracked"])

    def test_goal_without_task_is_404(self) -> None:
        goal = self._create_goal()
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.get_goal_diff(self.settings, goal["id"])
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "goal_no_task")

    def test_unknown_goal_is_404(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.get_goal_diff(self.settings, "goal-nope")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "goal_not_found")


class RepoExecRiskTests(_DiffFixture):
    """Repo-controlled config/attributes must never make the diff run a program.

    An agent in the worktree can write the shared `.git/config`, files it
    includes, `.git/info/attributes` and any `.gitattributes`. `git diff` /
    `git status` would hand worktree files to a `filter.<x>.clean` command, so
    the diff endpoints refuse with `409 unsafe_repo_config` before touching
    worktree content, and the filter's marker file is never created.
    """

    def setUp(self) -> None:
        super().setUp()
        self.marker = self.root / "filter-ran"
        self.filter_cmd = f"touch {shlex.quote(str(self.marker))}; cat"

    def _common_dir(self) -> Path:
        raw = _git(self.wt_path, "rev-parse", "--git-common-dir").strip()
        path = Path(raw)
        return path if path.is_absolute() else (self.wt_path / path).resolve()

    def _commit_subdir_attributes(self) -> str:
        """Tracked sub/data.txt + sub/.gitattributes naming the `pwn` filter."""
        base = _head(self.wt_path)
        (self.wt_path / "sub").mkdir()
        (self.wt_path / "sub" / "data.txt").write_text("one\n", encoding="utf-8")
        (self.wt_path / "sub" / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
        _git(self.wt_path, "add", "-A")
        _git(self.wt_path, "commit", "-qm", "sub dir with attributes")
        # Uncommitted change: `git diff <base>` must read (and filter) it.
        (self.wt_path / "sub" / "data.txt").write_text("one\ntwo\n", encoding="utf-8")
        return base

    def _assert_refused(self, task_id: str, expected_label: str) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.get_task_diff(self.settings, task_id)
        self.assertEqual(ctx.exception.status, 409)
        payload = ctx.exception.to_payload()
        self.assertEqual(payload.get("code"), "unsafe_repo_config")
        self.assertIn(expected_label, payload.get("error", "").lower())
        self.assertIn("为了安全", payload.get("error", ""))
        self.assertFalse(self.marker.exists(), "the planted filter must never run")

    def test_filter_in_repo_config_with_subdir_attributes_is_refused(self) -> None:
        base = self._commit_subdir_attributes()
        task_id = self._create_task(base_sha=base)
        _git(self.repo_dir, "config", "filter.pwn.clean", self.filter_cmd)

        self._assert_refused(task_id, "filter.pwn.clean")

        # Same through the HTTP route.
        from conveyor.web.web_supervisor_api import handle_get

        handler = _FakeHandler(self.settings)
        path = f"/api/supervisor/tasks/{task_id}/diff"
        handle_get(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        code, body = handler.response
        self.assertEqual(code, 409, body)
        self.assertEqual(body.get("code"), "unsafe_repo_config")
        self.assertFalse(self.marker.exists())

        # Sanity: the setup really is armed — a plain `git diff` fires the filter.
        subprocess.run(
            ["git", "-C", str(self.wt_path), "diff", base],
            capture_output=True, check=False,
        )
        self.assertTrue(self.marker.exists(), "fixture must be a live filter")

    def test_goal_diff_is_refused_too(self) -> None:
        base = self._commit_subdir_attributes()
        goal = goals.create_goal(
            None,
            self.settings,
            {"outcome": "读取目标 diff", "repo_id": self.repo_id, "worktree_id": self.worktree_id},
        )
        task_id = self._create_task(base_sha=base)
        goals._ensure_iteration(self.settings, goal["id"], 1, "第一轮")
        goals._set_iteration_task(self.settings, goal["id"], 1, task_id)
        _git(self.repo_dir, "config", "filter.pwn.clean", self.filter_cmd)
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.get_goal_diff(self.settings, goal["id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "unsafe_repo_config")
        self.assertFalse(self.marker.exists())

    def test_no_diff_or_status_runs_while_risky(self) -> None:
        base = self._commit_subdir_attributes()
        task_id = self._create_task(base_sha=base)
        _git(self.repo_dir, "config", "filter.pwn.clean", self.filter_cmd)
        seen: list[list[str]] = []
        real_git = diff_mod._git

        def _recording_git(settings: Any, worktree: str, args: list[str], **kwargs: Any):
            seen.append(list(args))
            return real_git(settings, worktree, args, **kwargs)

        with patch.object(diff_mod, "_git", _recording_git):
            with self.assertRaises(svc.SupervisorServiceError):
                svc.get_task_diff(self.settings, task_id)
        self.assertTrue(seen)
        for args in seen:
            self.assertNotIn(args[0], ("diff", "status", "add"), seen)
        self.assertFalse(self.marker.exists())

    def test_filter_defined_through_include_path_is_refused(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        included = self.root / "included.cfg"
        included.write_text(f'[filter "pwn"]\n\tclean = {self.filter_cmd}\n', encoding="utf-8")
        _git(self.repo_dir, "config", "include.path", str(included))
        (self.wt_path / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
        (self.wt_path / "tracked.txt").write_text("tracked baseline\nedited\n", encoding="utf-8")

        risks = diff_mod.repo_exec_risks(self.settings, str(self.wt_path))
        lowered = [r.lower() for r in risks]
        # `git config --local --list` would not see this one: the include is followed.
        self.assertIn("filter.pwn.clean", lowered)
        self.assertIn("include.path", lowered)
        self._assert_refused(task_id, "include.path")

    def test_driver_in_info_attributes_is_refused(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        info = self._common_dir() / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("*.txt filter=pwn\n", encoding="utf-8")
        (self.wt_path / "tracked.txt").write_text("tracked baseline\nedited\n", encoding="utf-8")
        self._assert_refused(task_id, "info/attributes")

    def test_untracked_and_ignored_attributes_files_are_found(self) -> None:
        (self.wt_path / "deep" / "er").mkdir(parents=True)
        (self.wt_path / "deep" / "er" / ".gitattributes").write_text(
            "*.c merge=evil\n", encoding="utf-8"
        )
        (self.wt_path / "hidden").mkdir()
        (self.wt_path / "hidden" / ".gitattributes").write_text(
            "*.txt filter=pwn\n", encoding="utf-8"
        )
        exclude = self._common_dir() / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("hidden/\n", encoding="utf-8")
        risks = diff_mod.repo_exec_risks(self.settings, str(self.wt_path))
        self.assertIn("deep/er/.gitattributes", risks)
        self.assertIn("hidden/.gitattributes", risks)

    def test_tracked_attributes_deleted_from_disk_still_counts(self) -> None:
        """git falls back to the index copy when the worktree file is missing."""
        self._commit_subdir_attributes()
        (self.wt_path / "sub" / ".gitattributes").unlink()
        risks = diff_mod.repo_exec_risks(self.settings, str(self.wt_path))
        self.assertIn("sub/.gitattributes", risks)

    def test_symlinked_attributes_file_is_refused(self) -> None:
        outside = self.root / "outside-attrs"
        outside.write_text("# harmless looking\n", encoding="utf-8")
        os.symlink(str(outside), str(self.wt_path / ".gitattributes"))
        self.assertIn(".gitattributes", diff_mod.repo_exec_risks(self.settings, str(self.wt_path)))

    def test_benign_repository_still_diffs(self) -> None:
        base = _head(self.wt_path)
        (self.wt_path / ".gitattributes").write_text(
            "# comment\n*.txt text eol=lf\n*.png -diff binary\n*.py diff=python\n"
            "*.lock merge=binary\n",
            encoding="utf-8",
        )
        _git(self.repo_dir, "config", "core.fsmonitor", "false")
        _git(self.repo_dir, "config", "core.hooksPath", "")
        _git(self.repo_dir, "config", "diff.noprefix", "true")
        self.assertEqual(diff_mod.repo_exec_risks(self.settings, str(self.wt_path)), [])
        (self.wt_path / "tracked.txt").write_text("tracked baseline\nedited\n", encoding="utf-8")
        payload = svc.get_task_diff(self.settings, self._create_task(base_sha=base))
        self.assertIn("+edited", payload["diff"])

    def test_risky_config_entry_classification(self) -> None:
        for key, value in (
            ("include.path", "/x"),
            ("includeIf.gitdir:/a.b/.path", "/x"),
            ("core.attributesFile", "/x"),
            ("core.fsmonitor", "/bin/echo"),
            ("core.hooksPath", "/x"),
            ("core.sshCommand", "ssh -x"),
            ("core.pager", "less"),
            ("diff.external", "x"),
            ("filter.x.clean", "x"),
            ("filter.x.required", "true"),
            ("merge.x.driver", "x"),
            ("merge.with.dots.driver", "x"),
            ("diff.x.textconv", "x"),
            ("diff.x.command", "x"),
            ("credential.helper", "store"),
            ("credential.https://example.com.helper", "store"),
            ("gpg.program", "x"),
            ("gpg.ssh.program", "x"),
            ("core.editor", "vim"),
            ("sequence.editor", "vim"),
        ):
            self.assertTrue(diff_mod.risky_config_entry(key, value), key)
        for key, value in (
            ("core.fsmonitor", "false"),
            ("core.hooksPath", ""),
            ("user.name", "x"),
            ("diff.noprefix", "true"),
            ("merge.log", "true"),
            ("remote.origin.url", "https://example.com/x.git"),
            ("core.autocrlf", "input"),
        ):
            self.assertFalse(diff_mod.risky_config_entry(key, value), key)

    def test_git_argv_forces_hooks_and_attributes_file_off(self) -> None:
        argv = diff_mod._git_argv(str(self.wt_path), ["ls-files"])
        self.assertIn("core.hooksPath=/dev/null", argv)
        self.assertIn("core.attributesFile=/dev/null", argv)
        self.assertLess(argv.index("core.attributesFile=/dev/null"), argv.index("ls-files"))


class DiffHttpTests(_DiffFixture):
    def _get(self, path: str, settings: Any = None) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_get

        handler = _FakeHandler(settings or self.settings)
        handle_get(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        return handler.response

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_post

        handler = _FakeHandler(self.settings)
        handle_post(handler, path, path.strip("/").split("/"), body)
        assert handler.response is not None
        return handler.response

    def test_task_diff_route(self) -> None:
        base = _head(self.wt_path)
        task_id = self._create_task(base_sha=base)
        self._make_candidate_changes()

        code, body = self._get(f"/api/supervisor/tasks/{task_id}/diff")

        self.assertEqual(code, 200, body)
        self.assertEqual(body["task_id"], task_id)
        self.assertEqual(body["base_sha"], base)
        self.assertIn("README.md", self._by_path(body))
        self.assertIn("notes.txt", body["untracked"])
        self.assertFalse(body["truncated"])

    def test_task_diff_route_rejects_missing_worktree(self) -> None:
        task_id = self._create_task(worktree_path=self.managed_root / "sup-vanished")
        code, body = self._get(f"/api/supervisor/tasks/{task_id}/diff")
        self.assertEqual(code, 404)
        self.assertEqual(body.get("code"), "worktree_missing")

    def test_task_diff_route_unknown_task(self) -> None:
        code, body = self._get("/api/supervisor/tasks/sup-nope/diff")
        self.assertEqual(code, 404)
        self.assertEqual(body.get("code"), "task_not_found")

    def test_goal_diff_route(self) -> None:
        code, created = self._post(
            "/api/supervisor/goals",
            {"outcome": "读取目标 diff", "repo_id": self.repo_id, "worktree_id": self.worktree_id},
        )
        self.assertEqual(code, 201, created)
        goal_id = str(created["id"])

        # No iteration task yet -> honest 404, not an empty diff.
        code, body = self._get(f"/api/supervisor/goals/{goal_id}/diff")
        self.assertEqual(code, 404)
        self.assertEqual(body.get("code"), "goal_no_task")

        # Simulate the first iteration task the goal loop would dispatch.
        task_id = self._create_task(base_sha=_head(self.wt_path))
        goals._ensure_iteration(self.settings, goal_id, 1, "第一轮：读取目标 diff")
        goals._set_iteration_task(self.settings, goal_id, 1, task_id)

        (self.wt_path / "goal-change.txt").write_text("goal change\n", encoding="utf-8")
        code, body = self._get(f"/api/supervisor/goals/{goal_id}/diff")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["goal_id"], goal_id)
        self.assertIn("goal-change.txt", body["untracked"])
        self.assertIn("+goal change", body["diff"])

    def test_diff_routes_require_feature_enabled(self) -> None:
        off_settings = dataclasses.replace(
            _make_settings(self.root / "off"),
            agent_supervisor_enabled=False,
        )
        code, body = self._get("/api/supervisor/tasks/sup-whatever/diff", off_settings)
        self.assertEqual(code, 409)
        self.assertEqual(body.get("code"), "agent_supervisor_disabled")

        code, goal_body = self._get("/api/supervisor/goals/goal-x/diff", off_settings)
        self.assertEqual(code, 409)
        self.assertEqual(goal_body.get("code"), "agent_supervisor_disabled")


if __name__ == "__main__":
    unittest.main()
