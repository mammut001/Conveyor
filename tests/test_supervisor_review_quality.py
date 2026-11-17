"""Tests for G1: deterministic diff leak scanner, reviewer prompt quality rules, and review flow."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conveyor.agents import agent_supervisor as ags
from conveyor.core.config import Settings


def _make_settings(
    tmp: Path,
    *,
    validation_commands: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp,
        codex_bin="codex",
        codex_task_root=tmp / "tasks",
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_analyst_executor="fake",
        agent_supervisor_implementer_executor="fake",
        agent_supervisor_reviewer_executor="fake",
        agent_supervisor_task_timeout_seconds=30,
        agent_supervisor_validation_commands=validation_commands,
        agent_supervisor_validation_timeout_seconds=15,
    )


def _git(*argv: str, cwd: Path) -> str:
    res = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {res.stderr[:300]}")
    return (res.stdout or "").strip()


def _make_repo_and_worktree(tmp: Path) -> tuple[Path, Path, str]:
    repo = tmp / "projects" / "demo-repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "g1@test.local", cwd=repo)
    _git("config", "user.name", "G1 Test", cwd=repo)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    wt = tmp / "tasks" / "worktrees" / "supervisor" / "sup-g1"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "--detach", str(wt), head, cwd=repo)
    return repo, wt, head


class _LeakFlowExecutor:
    """Scripted executor tracking role calls; supports failing round 0 via leak then fixing in round 1."""

    def __init__(
        self,
        worktree: Path,
        *,
        fix_on_repair: bool = False,
        leak_line: str = "仓库路径：/srv/conveyor/projects/qa-live-1115\n",
        untracked_only: bool = False,
    ) -> None:
        self.worktree = worktree
        self.fix_on_repair = fix_on_repair
        self.leak_line = leak_line
        self.untracked_only = untracked_only
        self.calls: list[str] = []
        self.impl_prompts: list[str] = []
        self._impl_count = 0

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def run(self, *, role: ags.RoleConfig, prompt: str, worktree_path: str, timeout_seconds: int) -> ags.ExecutionResult:
        self.calls.append(role.name)
        now = ags._utc_now()
        if role.name == "coordinator":
            return ags.ExecutionResult(
                success=True,
                output="Refine README intro task",
                started_at=now,
                finished_at=now,
                execution_id="exec-coord",
            )
        if role.name == "analyst":
            return ags.ExecutionResult(
                success=True,
                output="Add intro to README.md",
                started_at=now,
                finished_at=now,
                execution_id="exec-analyst",
            )
        if role.name == "implementer":
            self.impl_prompts.append(prompt)
            self._impl_count += 1
            if self._impl_count == 1 or not self.fix_on_repair:
                if self.untracked_only:
                    (self.worktree / "INTRO.md").write_text(self.leak_line, encoding="utf-8")
                else:
                    (self.worktree / "README.md").write_text(
                        "# Demo\n\n项目简介。\n" + self.leak_line,
                        encoding="utf-8",
                    )
                    _git("add", "README.md", cwd=self.worktree)
                    _git("commit", "-qm", "round 1 with leak", cwd=self.worktree)
            else:
                (self.worktree / "README.md").write_text("# Demo\n\n项目简介。\n", encoding="utf-8")
                _git("add", "README.md", cwd=self.worktree)
                _git("commit", "-qm", "round 2 fixed", cwd=self.worktree)
            ident = ags.target_identity(str(self.worktree))
            return ags.ExecutionResult(
                success=True,
                output="Updated README.md",
                started_at=now,
                finished_at=now,
                sha=ident["sha"],
                fingerprint=ident["fingerprint"],
                execution_id=f"exec-impl-{self._impl_count}",
            )
        if role.name == "reviewer":
            cur_sha, target_fp = "", ""
            for ln in prompt.splitlines():
                if ln.startswith("Current SHA: "):
                    cur_sha = ln.split("Current SHA: ", 1)[1].strip()
                elif ln.startswith("Target fingerprint: "):
                    target_fp = ln.split("Target fingerprint: ", 1)[1].strip()
            ident = ags.target_identity(str(self.worktree))
            verdict = {
                "passed": True,
                "target_sha": cur_sha or ident["sha"],
                "target_fingerprint": target_fp or ident["fingerprint"],
                "issues": [],
                "checks": [
                    {
                        "name": "readme-check",
                        "command": "cat README.md",
                        "passed": True,
                        "output": "clean",
                    }
                ],
                "summary": "内容清晰且无泄露。",
            }
            return ags.ExecutionResult(
                success=True,
                output=json.dumps(verdict, ensure_ascii=False),
                started_at=now,
                finished_at=now,
                sha=ident["sha"],
                fingerprint=ident["fingerprint"],
                execution_id="exec-rev",
            )
        raise AssertionError(f"unexpected role {role.name}")

    async def cancel(self, execution_id: str) -> bool:
        return False


class DiffLeakScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-g1-scan-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = _make_settings(self.tmp)
        self.repo_path = str(self.tmp / "repos" / "my-project")
        self.wt_path = str(self.tmp / "tasks" / "worktrees" / "supervisor" / "sup-abc123")

    def test_hits_for_internal_paths_roots_hostname_and_tokens(self) -> None:
        fake_gh = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        fake_sk = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
        fake_assign = 'api_key = "super-secret-value-9876"'
        host = "prod-conveyor-vps-01"
        mem_root = str(self.settings.codex_memory_root)
        task_root = str(self.settings.codex_task_root)
        proj_root = str(self.settings.codex_task_root / "projects")
        diff = "\n".join([
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -10,1 +10,9 @@",
            " context line 10",
            f"+worktree: {self.wt_path}",
            f"+repo: {self.repo_path}",
            "+srv: /srv/conveyor/projects/qa-live-1115",
            f"+mem: {mem_root}/agent_memory.db",
            f"+task: {task_root}/state.db",
            f"+proj: {proj_root}/app-1",
            f"+host: {host}",
            f"+gh: {fake_gh}",
            f"+openai: {fake_sk}",
            f"+cfg: {fake_assign}",
        ])
        issues = ags.scan_diff_for_leaks(
            diff,
            worktree_path=self.wt_path,
            repo_path=self.repo_path,
            settings=self.settings,
            hostname=host,
            goal="给 README 加一段项目介绍",
        )
        # Lines 11..20 -> 10 added lines, each should produce one hit.
        self.assertEqual(len(issues), 10, issues)
        self.assertIn("README.md 第 11 行", issues[0])
        self.assertIn(self.wt_path, issues[0])
        self.assertIn("README.md 第 12 行", issues[1])
        self.assertIn(self.repo_path, issues[1])
        self.assertIn("README.md 第 13 行", issues[2])
        self.assertIn("/srv/conveyor/…", issues[2])
        self.assertIn("README.md 第 14 行", issues[3])
        self.assertIn("README.md 第 15 行", issues[4])
        self.assertIn("README.md 第 16 行", issues[5])
        self.assertIn("README.md 第 17 行", issues[6])
        self.assertIn(host, issues[6])
        self.assertIn("README.md 第 18 行", issues[7])
        self.assertIn("README.md 第 19 行", issues[8])
        self.assertIn("README.md 第 20 行", issues[9])

        joined = "\n".join(issues)
        self.assertNotIn(fake_gh, joined)
        self.assertNotIn(fake_sk, joined)
        self.assertNotIn("super-secret-value-9876", joined)

    def test_socket_gethostname_used_by_default(self) -> None:
        diff = "\n".join([
            "diff --git a/docs/info.md b/docs/info.md",
            "--- /dev/null",
            "+++ b/docs/info.md",
            "@@ -0,0 +1,1 @@",
            "+deployed on mock-box-77",
        ])
        with patch("conveyor.agents.agent_supervisor.socket.gethostname", return_value="mock-box-77"):
            issues = ags.scan_diff_for_leaks(diff, hostname=None, goal="update docs")
        self.assertEqual(len(issues), 1)
        self.assertIn("docs/info.md 第 1 行", issues[0])
        self.assertIn("mock-box-77", issues[0])

    def test_allows_path_when_present_in_goal_text_only_for_that_path(self) -> None:
        diff = "\n".join([
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -1,0 +1,2 @@",
            "+允许的路径：/srv/conveyor/projects/allowed-demo",
            "+偷渡的路径：/srv/conveyor/worktrees/supervisor/sup-999",
        ])
        issues = ags.scan_diff_for_leaks(
            diff,
            goal="在 README 里说明项目位于 /srv/conveyor/projects/allowed-demo",
            hostname="",
        )
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("README.md 第 2 行", issues[0])
        self.assertIn("/srv/conveyor/…", issues[0])

    def test_ignores_removed_lines_and_git_internal_files(self) -> None:
        diff = "\n".join([
            "diff --git a/.git/config b/.git/config",
            "--- a/.git/config",
            "+++ b/.git/config",
            "@@ -1,0 +1,2 @@",
            f"+worktree = {self.wt_path}",
            "+token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -5,3 +5,2 @@",
            f"-old path: {self.repo_path}",
            "-old secret: sk-abcdefghijklmnopqrstuvwxyz0123456789",
            "+clean documentation line",
        ])
        issues = ags.scan_diff_for_leaks(
            diff,
            worktree_path=self.wt_path,
            repo_path=self.repo_path,
            settings=self.settings,
            hostname="host-99",
            goal="clean up README",
        )
        self.assertEqual(issues, [])


class ReviewerPromptAndFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-g1-flow-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = _make_repo_and_worktree(self.tmp)
        self._active_executor: _LeakFlowExecutor = _LeakFlowExecutor(self.wt)
        self._executor_patch = patch.object(
            ags, "get_executor", lambda _settings, _name: self._active_executor
        )
        self._executor_patch.start()
        self.addCleanup(self._executor_patch.stop)

    def test_reviewer_prompt_includes_server_date_and_quality_rules(self) -> None:
        ident = ags.target_identity(str(self.wt))
        today_str = datetime.now().strftime("%Y-%m-%d")
        prompt = ags.build_reviewer_prompt(
            goal="给 README 加一段项目介绍",
            worktree_path=str(self.wt),
            base_sha=self.head,
            current_sha=ident["sha"],
            fingerprint=ident["fingerprint"],
            analysis_plan="Update README",
            implementer_summary="Updated README",
            acceptance_criteria="README accurate",
        )
        self.assertIn(f"Today is {today_str} (server date). Dates written into files must not contradict it.", prompt)
        self.assertIn("Content-quality rule:", prompt)
        self.assertIn("Usefulness rule:", prompt)
        self.assertIn("does not move the stated goal forward", prompt)

    def test_leak_fails_round_without_calling_model_reviewer_then_repairs(self) -> None:
        val_cmd = ("check-readme", (sys.executable, "-c", "import pathlib; assert pathlib.Path('README.md').is_file()"))
        settings = _make_settings(self.tmp, validation_commands=(val_cmd,))
        executor = _LeakFlowExecutor(self.wt, fix_on_repair=True)
        self._active_executor = executor
        task = ags.create_task(
            settings,
            goal="给 README 加一段项目介绍",
            repo_path=str(self.repo),
            worktree_path=str(self.wt),
            base_sha=self.head,
        )
        ags.queue_task(settings, task["id"], worktree_path=str(self.wt))
        ags.claim_task(settings)
        final = asyncio.run(ags.run_supervisor_flow(settings, task["id"], max_repairs=1))

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["repair_round"], 1)
        # Round 0 leaked -> reviewer was NOT called; round 1 repaired -> reviewer called once.
        self.assertEqual(executor.calls, ["coordinator", "analyst", "implementer", "implementer", "reviewer"])
        self.assertIn("写进了服务器内部路径 /srv/conveyor/…", executor.impl_prompts[1])

        acc = ags.effective_acceptance(settings, task["id"])
        self.assertIsNotNone(acc)
        self.assertTrue(acc["passed"])
        self.assertEqual(acc["evidence_source"], "operator_validation+independent_review")
        self.assertTrue(acc["provenance_verified"])

    def test_leak_without_repair_budget_fails_without_calling_model_reviewer(self) -> None:
        settings = _make_settings(self.tmp, validation_commands=())
        executor = _LeakFlowExecutor(self.wt, fix_on_repair=False, untracked_only=True)
        self._active_executor = executor
        task = ags.create_task(
            settings,
            goal="给 README 加一段项目介绍",
            repo_path=str(self.repo),
            worktree_path=str(self.wt),
            base_sha=self.head,
        )
        ags.queue_task(settings, task["id"], worktree_path=str(self.wt))
        ags.claim_task(settings)
        final = asyncio.run(ags.run_supervisor_flow(settings, task["id"], max_repairs=0))

        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["stage"], "repair_limit_exceeded")
        # Model reviewer was never invoked because deterministic leak check caught the untracked file leak.
        self.assertEqual(executor.calls, ["coordinator", "analyst", "implementer"])
        acc = ags.latest_acceptance(settings, task["id"])
        self.assertIsNotNone(acc)
        self.assertFalse(acc["passed"])
        self.assertTrue(any("INTRO.md 第 1 行写进了服务器内部路径 /srv/conveyor/…" in msg for msg in acc["issues"]))


if __name__ == "__main__":
    unittest.main()
