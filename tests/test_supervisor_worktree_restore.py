"""tests/test_supervisor_worktree_restore.py — 工作目录的文件夹丢了，改动还在分支上。

Real temp git repos with an explicit identity, a real managed worktree created
through `supervisor_service.create_managed_worktree`, and a real `shutil.rmtree`
of that worktree's folder — the exact shape of the VPS incident where a bug
deleted `<task_root>/worktrees/supervisor/` wholesale. No model API or CLI is
ever called.

Covers: a managed worktree whose folder is gone stays listed (`state: "missing"`,
its branch, `ahead_of_base`) instead of disappearing; restore recreates it at the
very same path with the same HEAD so goals/tasks/chat bindings keep working; a
path that cannot be reused falls back to a fresh one under the same supervisor
root and the stored path is updated; restore refuses when the branch is gone;
「合并到主分支」 answers with the one actionable Chinese sentence instead of a git
error; and the HTTP route (owner-only, same auth/Origin pattern as the other
supervisor write routes).
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import supervisor_merge as merge_mod
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
        conveyor_web_token="restore-test-token-0123456789-abcdefghijklmnop",
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
    (repo_dir / "README.md").write_text("# Disposable Restore Repo\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "initial commit")
    return _head(repo_dir)


class _FakeHandler:
    """Minimal WebConsoleHandler stand-in for direct route dispatch tests."""

    def __init__(self, settings: Any, headers: dict[str, str] | None = None) -> None:
        self.server = types.SimpleNamespace(
            control=types.SimpleNamespace(settings=settings),
            loop=None,
        )
        self.headers = headers if headers is not None else {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.response: tuple[int, dict[str, Any]] | None = None

    def _json(self, status: Any, payload: dict[str, Any]) -> None:
        self.response = (int(status), payload)


class _RestoreFixture(unittest.TestCase):
    """One real repo + one real managed worktree whose folder then gets deleted."""

    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-restore-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="sup/114aa945")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = Path(str(wt["path"]))
        self.main_worktree_id = next(
            str(w["id"]) for w in repo["worktrees"] if w["is_main"]
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _worktrees(self) -> dict[str, dict[str, Any]]:
        repos = svc.list_repositories(self.settings)
        return {str(w["id"]): w for w in repos[0]["worktrees"]}

    def _commit_in_worktree(self, message: str = "candidate work") -> str:
        (self.wt_path / "tracked.txt").write_text(
            "tracked baseline\nworktree side\n", encoding="utf-8"
        )
        (self.wt_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        _git(self.wt_path, "add", "-A")
        _git(self.wt_path, "commit", "-qm", message)
        return _head(self.wt_path)

    def _delete_worktree_folder(self) -> str:
        """The VPS incident: the whole folder is gone, the branch is not."""
        head = _head(self.wt_path)
        shutil.rmtree(self.wt_path)
        return head

    def _delete_branch(self, branch: str) -> None:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_dir), "branch", "-D", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            # git refuses to drop a branch another worktree has checked out; the
            # plumbing form is what a wiped `.git/worktrees/<n>` leaves behind.
            subprocess.run(
                ["git", "-C", str(self.repo_dir), "update-ref", "-d", f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                check=True,
            )

    def _stored_row(self, worktree_id: str | None = None) -> dict[str, Any]:
        conn = svc._connect(self.settings)
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_worktrees WHERE id = ?",
                (worktree_id or self.worktree_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else {}

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_post

        handler = _FakeHandler(self.settings)
        handle_post(handler, path, path.strip("/").split("/"), body)
        assert handler.response is not None
        return handler.response

    def _get(self, path: str) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_get

        handler = _FakeHandler(self.settings)
        handle_get(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        return handler.response


class MissingWorktreeListingTests(_RestoreFixture):
    def test_deleted_folder_stays_listed_as_missing_with_its_branch(self) -> None:
        head = self._commit_in_worktree()
        self._delete_worktree_folder()

        wts = self._worktrees()
        self.assertIn(self.worktree_id, wts)
        item = wts[self.worktree_id]
        self.assertEqual(item["state"], "missing")
        self.assertTrue(item["missing"])
        self.assertEqual(item["branch"], "sup/114aa945")
        self.assertEqual(item["head"], head)
        self.assertTrue(item["managed"])
        self.assertFalse(item["is_main"])
        self.assertEqual(item["ahead_of_base"], 1)
        self.assertEqual(item["path"], str(self.wt_path))

        # The row and the branch are both still there: nothing was auto-deleted.
        row = self._stored_row()
        self.assertEqual(row["canonical_path"], str(self.wt_path))
        self.assertEqual(row["branch"], "sup/114aa945")
        self.assertTrue(_branch_exists(self.repo_dir, "sup/114aa945"))

    def test_main_and_unmanaged_worktrees_are_untouched(self) -> None:
        ext_wt = self.root / "outside-wt"
        _git(self.repo_dir, "worktree", "add", "-b", "ext-branch", str(ext_wt))
        self._commit_in_worktree()
        self._delete_worktree_folder()

        wts = self._worktrees()
        self.assertEqual(wts[self.main_worktree_id]["state"], "ready")
        self.assertTrue(wts[self.main_worktree_id]["is_main"])
        ext = next(w for w in wts.values() if w["path"] == str(ext_wt.resolve()))
        self.assertEqual(ext["state"], "ready")
        self.assertFalse(ext["managed"])

        # Deleting an *unmanaged* worktree's folder does not resurrect it.
        shutil.rmtree(ext_wt)
        wts = self._worktrees()
        self.assertNotIn(str(ext["id"]), wts)
        self.assertIn(self.worktree_id, wts)

    def test_missing_worktree_is_listed_even_after_its_admin_entry_is_pruned(self) -> None:
        """A wiped `.git/worktrees/<n>` must not lose the owner's work either."""
        self._commit_in_worktree()
        self._delete_worktree_folder()
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=True,
        )

        wts = self._worktrees()
        self.assertIn(self.worktree_id, wts)
        self.assertEqual(wts[self.worktree_id]["state"], "missing")
        self.assertEqual(wts[self.worktree_id]["branch"], "sup/114aa945")

    def test_a_branch_that_is_gone_is_not_listed(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()
        self._delete_branch("sup/114aa945")

        self.assertNotIn(self.worktree_id, self._worktrees())
        # Still never deleted from the database.
        self.assertEqual(self._stored_row()["id"], self.worktree_id)

    def test_a_folder_that_is_back_is_not_listed_as_missing(self) -> None:
        self._commit_in_worktree()
        head = self._delete_worktree_folder()
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "worktree", "add", str(self.wt_path), "sup/114aa945"],
            capture_output=True,
            text=True,
            check=True,
        )

        wts = self._worktrees()
        self.assertEqual(wts[self.worktree_id]["state"], "ready")
        self.assertFalse(wts[self.worktree_id]["missing"])
        self.assertEqual(wts[self.worktree_id]["head"], head)


class RestoreWorktreeTests(_RestoreFixture):
    def test_restore_recreates_the_same_path_with_the_same_head(self) -> None:
        head = self._commit_in_worktree()
        self._delete_worktree_folder()

        restored = svc.restore_missing_worktree(self.settings, self.worktree_id)

        self.assertEqual(restored["id"], self.worktree_id)
        self.assertEqual(restored["path"], str(self.wt_path))
        self.assertEqual(restored["state"], "ready")
        self.assertFalse(restored["missing"])
        self.assertTrue(restored["managed"])
        self.assertEqual(restored["branch"], "sup/114aa945")
        self.assertTrue(Path(self.wt_path).is_dir())
        self.assertEqual(_head(self.wt_path), head)
        self.assertEqual((self.wt_path / "calc.py").read_text(encoding="utf-8"),
                         "def add(a, b):\n    return a + b\n")
        self.assertEqual(restored["head"], head)

        # The stored path did not move, so goals / tasks / chat keep pointing at it.
        self.assertEqual(self._stored_row()["canonical_path"], str(self.wt_path))
        self.assertEqual(restored["chat_session_id"], self._stored_row()["chat_session_id"])

        # git agrees it is a real worktree of this repository again.
        self.assertIn(str(self.wt_path.resolve()), _git(self.repo_dir, "worktree", "list", "--porcelain"))

    def test_restore_keeps_the_worktree_usable(self) -> None:
        head = self._commit_in_worktree()
        self._delete_worktree_folder()
        svc.restore_missing_worktree(self.settings, self.worktree_id)

        ok, reason = ags.validate_worktree(self.settings, str(self.repo_dir), str(self.wt_path))
        self.assertTrue(ok, reason)
        self.assertEqual(_head(self.wt_path), head)

        # A restored worktree can be merged again (the incident is fully undone).
        preview = merge_mod.build_merge_preview(self.settings, self.worktree_id)
        self.assertTrue(preview["can_merge"], preview)
        self.assertEqual(preview["commits_ahead"], 1)
        self.assertEqual(preview["blockers"], [])

    def test_restore_is_idempotent_and_refuses_when_the_folder_is_back(self) -> None:
        self._commit_in_worktree()
        head = self._delete_worktree_folder()
        first = svc.restore_missing_worktree(self.settings, self.worktree_id)
        self.assertEqual(first["path"], str(self.wt_path))

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.restore_missing_worktree(self.settings, self.worktree_id)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "worktree_not_missing")
        self.assertEqual(_head(self.wt_path), head)

    def test_restore_refuses_when_the_branch_is_gone(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()
        self._delete_branch("sup/114aa945")

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.restore_missing_worktree(self.settings, self.worktree_id)
        exc = ctx.exception
        self.assertEqual(exc.status, 409)
        self.assertEqual(exc.to_payload().get("code"), "branch_missing")
        self.assertIn("分支", exc.to_payload().get("error", ""))
        # Nothing was created, nothing was deleted.
        self.assertFalse(Path(self.wt_path).exists())
        self.assertEqual(self._stored_row()["canonical_path"], str(self.wt_path))

    def test_restore_moves_to_a_fresh_path_when_the_old_one_is_taken(self) -> None:
        head = self._commit_in_worktree()
        self._delete_worktree_folder()
        # Something else took the exact spot: a plain file, not a directory.
        self.wt_path.write_text("occupied\n", encoding="utf-8")

        restored = svc.restore_missing_worktree(self.settings, self.worktree_id)

        self.assertNotEqual(restored["path"], str(self.wt_path))
        managed_root = Path(str(self.settings.codex_task_root)) / "worktrees" / "supervisor"
        self.assertEqual(Path(restored["path"]).parent, managed_root)
        self.assertTrue(Path(restored["path"]).is_dir())
        self.assertEqual(_head(Path(restored["path"])), head)
        self.assertEqual(restored["state"], "ready")

        # The stored path followed the move, so references keep working.
        self.assertEqual(self._stored_row()["canonical_path"], restored["path"])

        wts = self._worktrees()
        self.assertEqual(wts[self.worktree_id]["path"], restored["path"])
        self.assertEqual(wts[self.worktree_id]["state"], "ready")

    def test_restore_unknown_worktree_is_404(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.restore_missing_worktree(self.settings, "wt-does-not-exist")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.to_payload().get("code"), "worktree_not_found")

    def test_restore_refuses_the_main_checkout(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.restore_missing_worktree(self.settings, self.main_worktree_id)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.to_payload().get("code"), "unmanaged_worktree")


class GoalEndReportWhileMissingTests(_RestoreFixture):
    def test_finish_report_still_names_the_branch_and_the_commits(self) -> None:
        from conveyor.agents import supervisor_goals as goals

        self._commit_in_worktree()
        goal = goals.create_goal(
            None,
            self.settings,
            {
                "outcome": "整理计算器逻辑",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        self._delete_worktree_folder()

        text, scope = goals._build_finish_report_bundle(
            self.settings, goal, state=goals.GOAL_ACHIEVED
        )
        self.assertEqual(scope["branch"], "sup/114aa945")
        self.assertEqual(scope["commits_ahead"], 1)
        self.assertIn("改动在工作目录 sup/114aa945，还没有合并到主分支", text)


class TaskCreationWhileMissingTests(_RestoreFixture):
    def test_creating_a_task_says_restore_it(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            svc.create_scoped_task(self.settings, {
                "goal": "继续改计算器",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            })
        exc = ctx.exception
        self.assertEqual(exc.status, 409)
        self.assertEqual(exc.to_payload().get("code"), "worktree_folder_missing")
        self.assertEqual(exc.to_payload().get("error"), svc.WORKTREE_FOLDER_MISSING_MESSAGE)

        # 恢复之后同一件事就能做了。
        svc.restore_missing_worktree(self.settings, self.worktree_id)
        task, created = svc.create_scoped_task(self.settings, {
            "goal": "继续改计算器",
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
        })
        self.assertTrue(created)
        self.assertEqual(task["state"], "pending_dispatch")


class MergeWhileMissingTests(_RestoreFixture):
    def test_preview_says_restore_it_and_names_the_branch(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()

        preview = merge_mod.build_merge_preview(self.settings, self.worktree_id)

        self.assertFalse(preview["can_merge"])
        self.assertEqual(
            [b["code"] for b in preview["blockers"]], ["worktree_folder_missing"]
        )
        self.assertEqual(
            preview["blockers"][0]["message"],
            "工作目录的文件夹丢了，请先在 项目 里点「恢复工作目录」",
        )
        self.assertEqual(preview["source_branch"], "sup/114aa945")
        self.assertEqual(preview["target_branch"], "main")
        self.assertEqual(preview["commits_ahead"], 1)
        self.assertEqual(preview["files"], [])
        self.assertFalse(preview["has_uncommitted"])

    def test_merge_answers_with_the_same_sentence(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()
        main_head_before = _head(self.repo_dir)

        with self.assertRaises(merge_mod.SupervisorMergeError) as ctx:
            merge_mod.merge_worktree(self.settings, self.worktree_id, {"confirm": True})
        exc = ctx.exception
        self.assertEqual(exc.status, 409)
        self.assertEqual(exc.to_payload().get("code"), "worktree_folder_missing")
        self.assertEqual(
            exc.to_payload().get("error"),
            "工作目录的文件夹丢了，请先在 项目 里点「恢复工作目录」",
        )
        self.assertEqual(_head(self.repo_dir), main_head_before)
        self.assertEqual(_git(self.repo_dir, "status", "--porcelain").strip(), "")
        self.assertEqual(merge_mod.list_merge_audit(self.settings), [])


class RestoreHttpTests(_RestoreFixture):
    def test_restore_route(self) -> None:
        head = self._commit_in_worktree()
        self._delete_worktree_folder()

        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/restore", {}
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["id"], self.worktree_id)
        self.assertEqual(body["path"], str(self.wt_path))
        self.assertEqual(body["state"], "ready")
        self.assertFalse(body["missing"])
        self.assertEqual(_head(self.wt_path), head)

    def test_restore_route_requires_the_same_site_headers(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()

        from conveyor.web.web_supervisor_api import handle_post

        path = f"/api/supervisor/worktrees/{self.worktree_id}/restore"
        handler = _FakeHandler(self.settings, headers={})
        handle_post(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        self.assertEqual(handler.response[0], 403)
        self.assertEqual(handler.response[1].get("code"), "origin_or_request_denied")
        self.assertFalse(Path(self.wt_path).exists())

    def test_restore_route_unknown_worktree_is_404(self) -> None:
        code, body = self._post("/api/supervisor/worktrees/wt-nope/restore", {})
        self.assertEqual(code, 404)
        self.assertEqual(body.get("code"), "worktree_not_found")

    def test_restore_route_requires_feature_enabled(self) -> None:
        off_settings = dataclasses.replace(
            _make_settings(self.root / "off"),
            agent_supervisor_enabled=False,
        )
        from conveyor.web.web_supervisor_api import handle_post

        path = f"/api/supervisor/worktrees/{self.worktree_id}/restore"
        handler = _FakeHandler(off_settings)
        handle_post(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.response[1].get("code"), "agent_supervisor_disabled")

    def test_merge_preview_route_reports_the_missing_folder(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()

        code, body = self._get(f"/api/supervisor/worktrees/{self.worktree_id}/merge-preview")
        self.assertEqual(code, 200, body)
        self.assertFalse(body["can_merge"])
        self.assertEqual(
            [b["code"] for b in body["blockers"]], ["worktree_folder_missing"]
        )
        self.assertEqual(
            body["blockers"][0]["message"],
            "工作目录的文件夹丢了，请先在 项目 里点「恢复工作目录」",
        )

    def test_merge_route_reports_the_missing_folder(self) -> None:
        self._commit_in_worktree()
        self._delete_worktree_folder()

        code, body = self._post(
            f"/api/supervisor/worktrees/{self.worktree_id}/merge", {"confirm": True}
        )
        self.assertEqual(code, 409, body)
        self.assertEqual(body.get("code"), "worktree_folder_missing")
        self.assertEqual(
            body.get("error"),
            "工作目录的文件夹丢了，请先在 项目 里点「恢复工作目录」",
        )


def _branch_exists(repo_dir: Path, branch: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


if __name__ == "__main__":
    unittest.main()
