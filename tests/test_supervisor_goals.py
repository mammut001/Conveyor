"""tests/test_supervisor_goals.py — 目标闭环（goal-driven loop）unit tests.

Uses a disposable state root, a real disposable git repository with a real
managed worktree, the real `supervisor_service` create/dispatch functions, and a
fake coordinator chat executor. No real model API or CLI is ever called.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agent_supervisor as ags
import supervisor_goals as goals
import supervisor_service as svc
from config import Settings

OUTCOME = "在 managed worktree 里新增 feature.txt，内容为 hello-goal，并干净提交"

# A Chinese sentence long enough to exercise the 40-character title cut.
TITLE_BASE = "把首页加载时间降到一秒以内并且保证所有测试都通过"


def _chinese_outcome(length: int) -> str:
    """A single-line Simplified-Chinese outcome of exactly `length` characters."""
    return (TITLE_BASE * ((length // len(TITLE_BASE)) + 1))[:length]


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
        conveyor_web_token="goal-test-token-0123456789-abcdefghijklmnop",
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


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Supervisor Test"], check=True)
    (repo_dir / "README.md").write_text("# Disposable Goal Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class FakeChatExecutor:
    """Stand-in for every supervisor executor: always available, scripted output."""

    name = "fake"

    def __init__(self, settings: Any, responder: Any) -> None:
        self.settings = settings
        self._responder = responder
        self.calls = 0
        self.prompts: list[str] = []

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def run(
        self,
        *,
        role: Any,
        prompt: str,
        worktree_path: str,
        timeout_seconds: int,
        task_id: str = "",
        context: Any = None,
    ) -> ags.ExecutionResult:
        self.calls += 1
        self.prompts.append(prompt)
        await asyncio.sleep(0.05)
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


class SupervisorGoalTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in (
            "CONVEYOR_AGENT_SUPERVISOR_ENABLED",
            "CONVEYOR_SUPERVISOR_ENABLED",
            "CONVEYOR_CLAUDE_BIN",
            "CONVEYOR_COMMAND_CODE_BIN",
            "CONVEYOR_ANTIGRAVITY_BIN",
            "CONVEYOR_MAX_WORKTREES_BYTES",
        ):
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-goals-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        self.responder = self._default_responder
        self.fake = FakeChatExecutor(self.settings, lambda prompt: self.responder(prompt))
        self._executor_patch = patch.object(ags, "get_executor", self._fake_get_executor)
        self._executor_patch.start()
        self.addCleanup(self._executor_patch.stop)

        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/goal-loop")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = str(wt["path"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _fake_get_executor(self, _settings: Any, _name: str) -> Any:
        return self.fake

    def _default_responder(self, prompt: str) -> str:
        return json.dumps(
            {
                "decision": "achieved",
                "reason": "目标已经实现",
                "next_prompt": "",
                "question": "",
                "summary": "第一轮已达成目标。",
            },
            ensure_ascii=False,
        )

    def _respond_with(self, payload: dict[str, Any] | str) -> None:
        if isinstance(payload, str):
            self.responder = lambda _prompt: payload
        else:
            self.responder = lambda _prompt: json.dumps(payload, ensure_ascii=False)

    def _create_goal(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "outcome": OUTCOME,
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
        }
        body.update(overrides)
        return goals.create_goal(None, self.settings, body)

    def _advance(self) -> None:
        asyncio.run(goals.advance_goals(None, self.settings))

    def _goal(self, goal_id: str) -> dict[str, Any]:
        return goals.get_goal(self.settings, goal_id)

    def _current_task_id(self, goal_id: str) -> str:
        iterations = self._goal(goal_id)["iterations"]
        self.assertTrue(iterations, "goal must have at least one iteration")
        return str(iterations[-1]["task_id"])

    def _set_task_state(self, task_id: str, state: str, stage: str = "") -> None:
        if state == "completed":
            # `transition()` demands a passing acceptance whose sha/fingerprint match
            # the live worktree target, so feed it the real identity.
            identity = ags.target_identity(self.wt_path)
            with patch.object(
                ags,
                "effective_acceptance",
                return_value={
                    "passed": True,
                    "sha": identity["sha"],
                    "fingerprint": identity["fingerprint"],
                    "evidence_source": "manual_or_direct",
                    "issues": [],
                },
            ):
                ags.transition(self.settings, task_id, "completed", stage=stage or "completed")
            return
        ags.transition(self.settings, task_id, state, stage=stage or state)

    # ------------------------------------------------------------------
    # 1. create → iteration 1 dispatched
    # ------------------------------------------------------------------

    def test_create_goal_dispatches_first_iteration(self) -> None:
        goal = self._create_goal()
        self.assertEqual(goal["state"], "running")
        self.assertEqual(goal["iterations"], [])
        self.assertEqual(
            goal["title"], OUTCOME[: goals.MAX_TITLE_CHARS] + goals.TITLE_ELLIPSIS
        )
        self.assertNotIn("[truncated]", goal["title"])
        self.assertEqual(goal["max_iterations"], 5)

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(len(detail["iterations"]), 1)
        iteration = detail["iterations"][0]
        self.assertEqual(iteration["n"], 1)
        self.assertTrue(iteration["task_id"])
        self.assertIn(goals.FIRST_ITERATION_NOTE, iteration["prompt"])
        self.assertIn(OUTCOME, iteration["prompt"])

        task = ags.get_task(self.settings, iteration["task_id"]) or {}
        self.assertIn(task.get("state"), ("queued", "dispatching"))
        self.assertEqual(task.get("worktree_path"), self.wt_path)

    def test_create_goal_clamps_max_iterations_and_stores_role_overrides(self) -> None:
        high = self._create_goal(max_iterations=99)
        self.assertEqual(high["max_iterations"], 20)
        low = self._create_goal(max_iterations=0)
        self.assertEqual(low["max_iterations"], 1)
        goals.stop_goal(None, self.settings, high["id"])
        goals.stop_goal(None, self.settings, low["id"])

        with_roles = self._create_goal(role_overrides={"implementer": {"executor": "claude"}})
        self.assertEqual(
            with_roles["role_overrides"], {"implementer": {"executor": "claude"}}
        )
        self._advance()
        task_id = self._current_task_id(with_roles["id"])
        task = ags.get_task(self.settings, task_id) or {}
        roles = (task.get("metadata") or {}).get("roles") or {}
        self.assertEqual(roles.get("implementer", {}).get("executor"), "claude")

    # ------------------------------------------------------------------
    # 1b. title: first non-empty line, whitespace collapsed, 40 chars + 「…」
    # ------------------------------------------------------------------

    def test_title_cuts_81_char_chinese_outcome_at_40_with_ellipsis(self) -> None:
        outcome = _chinese_outcome(81)
        self.assertEqual(len(outcome), 81)
        goal = self._create_goal(outcome=outcome)
        self.assertEqual(goal["title"], outcome[: goals.MAX_TITLE_CHARS] + goals.TITLE_ELLIPSIS)
        self.assertEqual(len(goal["title"]), goals.MAX_TITLE_CHARS + 1)
        self.assertTrue(goal["title"].endswith(goals.TITLE_ELLIPSIS))
        # The old `truncate()` marker must never reach the owner.
        self.assertNotIn("[truncated]", goal["title"])
        self.assertNotIn("\n", goal["title"])

    def test_title_uses_first_non_empty_line_of_multiline_outcome(self) -> None:
        outcome = "把首页加载时间降到一秒以内\n顺便把所有测试跑过\n第三行只是补充说明"
        goal = self._create_goal(outcome=outcome)
        self.assertEqual(goal["title"], "把首页加载时间降到一秒以内")

    def test_title_keeps_short_outcome_untouched(self) -> None:
        goal = self._create_goal(outcome="修好登录页的报错")
        self.assertEqual(goal["title"], "修好登录页的报错")

    def test_title_from_outcome_skips_blank_lines_and_collapses_whitespace(self) -> None:
        self.assertEqual(
            goals._title_from_outcome("\n\n  把   构建  时间\n压到 30 秒以内"),
            "把 构建 时间",
        )
        self.assertEqual(goals._title_from_outcome("   "), "")
        self.assertEqual(
            goals._title_from_outcome(_chinese_outcome(81)),
            _chinese_outcome(81)[: goals.MAX_TITLE_CHARS] + goals.TITLE_ELLIPSIS,
        )

    def test_clean_title_falls_back_to_outcome_and_never_emits_truncate_marker(self) -> None:
        long_outcome = _chinese_outcome(81)
        for raw in (None, "", "   "):
            title = goals._clean_title(raw, long_outcome)
            self.assertEqual(title, long_outcome[: goals.MAX_TITLE_CHARS] + goals.TITLE_ELLIPSIS)
            self.assertNotIn("[truncated]", title)
        # An explicit title is collapsed the same way instead of being truncated.
        self.assertEqual(goals._clean_title("  手动   标题  ", long_outcome), "手动 标题")
        self.assertEqual(
            goals._clean_title(_chinese_outcome(81), long_outcome),
            _chinese_outcome(81)[: goals.MAX_TITLE_CHARS] + goals.TITLE_ELLIPSIS,
        )

    # ------------------------------------------------------------------
    # 2. completed + achieved → achieved
    # ------------------------------------------------------------------

    def test_completed_task_with_achieved_decision_marks_goal_achieved(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "achieved")
        self.assertEqual(detail["iterations"][0]["decision"], "achieved")
        self.assertEqual(detail["iterations"][0]["task_final_state"], "completed")
        self.assertTrue(detail["summary"])
        self.assertEqual(len(detail["iterations"]), 1)

    # ------------------------------------------------------------------
    # 3. failed + achieved from model → downgraded to continue → iteration 2
    # ------------------------------------------------------------------

    def test_failed_task_with_achieved_decision_downgrades_to_continue(self) -> None:
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "看起来已经完成",
                "next_prompt": "补充单元测试并重新运行验证",
                "question": "",
                "summary": "第一轮改动完成但验证失败。",
            }
        )
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "failed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "running")
        self.assertEqual(len(detail["iterations"]), 2)
        self.assertEqual(detail["iterations"][0]["decision"], "continue")
        self.assertEqual(detail["iterations"][0]["task_final_state"], "failed")
        second = detail["iterations"][1]
        self.assertEqual(second["n"], 2)
        self.assertIn("第 2 轮", second["prompt"])
        self.assertIn("补充单元测试并重新运行验证", second["prompt"])
        self.assertTrue(second["task_id"], "iteration 2 must be dispatched")
        self.assertGreaterEqual(self.fake.calls, 1)

    # ------------------------------------------------------------------
    # 4. budget exhausted
    # ------------------------------------------------------------------

    def test_budget_exhaustion_stops_the_goal(self) -> None:
        self._respond_with(
            {
                "decision": "continue",
                "reason": "还差一点",
                "next_prompt": "继续修改 feature.txt",
                "question": "",
                "summary": "继续迭代。",
            }
        )
        goal = self._create_goal(max_iterations=1)
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "exhausted")
        self.assertEqual(len(detail["iterations"]), 1)
        self.assertEqual(detail["iterations"][0]["decision"], "continue")
        self.assertIn("预算", detail["stop_reason"])

    def test_give_up_decision_fails_the_goal(self) -> None:
        self._respond_with(
            {
                "decision": "give_up",
                "reason": "仓库结构不支持该目标",
                "next_prompt": "",
                "question": "",
                "summary": "无法达成。",
            }
        )
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "failed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "failed")
        self.assertEqual(detail["iterations"][0]["decision"], "give_up")
        self.assertEqual(detail["stop_reason"], "仓库结构不支持该目标")

    # ------------------------------------------------------------------
    # 5. waiting_user (needs_validation) → goal waiting_user
    # ------------------------------------------------------------------

    def test_needs_validation_task_puts_goal_in_waiting_user(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "waiting_user", stage="needs_validation")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "waiting_user")
        self.assertEqual(detail["question"], goals.NEEDS_VALIDATION_QUESTION)
        self.assertEqual(detail["iterations"][0]["decision"], "ask_user")

    # ------------------------------------------------------------------
    # 6/7. reply achieved / reply message
    # ------------------------------------------------------------------

    def test_reply_achieved_true_confirms_goal(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "waiting_user", stage="needs_validation")
        self._advance()

        replied = goals.reply_goal(None, self.settings, goal["id"], {"achieved": True})
        self.assertEqual(replied["state"], "achieved")
        self.assertEqual(replied["stop_reason"], "用户确认已达成")

    def test_reply_message_starts_next_iteration_with_user_note(self) -> None:
        self._respond_with(
            {
                "decision": "continue",
                "reason": "还需要补充",
                "next_prompt": "把 feature.txt 改成 hello-goal-v2",
                "question": "",
                "summary": "第一轮完成。",
            }
        )
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")

        replied = goals.reply_goal(
            None, self.settings, goal["id"], {"message": "顺便更新 README", "achieved": False}
        )
        self.assertEqual(replied["state"], "running")
        self.assertEqual(len(replied["iterations"]), 2)
        second = replied["iterations"][1]
        self.assertIn("用户补充：顺便更新 README", second["prompt"])
        # The waiting task must have been cancelled so the worktree lock is released.
        cancelled_task = ags.get_task(self.settings, task_id) or {}
        self.assertEqual(cancelled_task.get("state"), "cancelled")

        self._advance()
        detail = self._goal(goal["id"])
        self.assertTrue(detail["iterations"][1]["task_id"], "reply iteration must be dispatched")
        task2 = ags.get_task(self.settings, detail["iterations"][1]["task_id"]) or {}
        self.assertIn(task2.get("state"), ("queued", "dispatching"))

    def test_reply_builds_on_evaluator_last_next_prompt(self) -> None:
        self._respond_with(
            {
                "decision": "ask_user",
                "reason": "需要用户确认技术选型",
                "next_prompt": "按方案 A 重构入口",
                "question": "你倾向方案 A 还是方案 B？",
                "summary": "等待用户选型。",
            }
        )
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")
        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "waiting_user")
        self.assertEqual(detail["question"], "你倾向方案 A 还是方案 B？")

        replied = goals.reply_goal(None, self.settings, goal["id"], {"message": "用方案 A"})
        second = replied["iterations"][1]
        self.assertIn("按方案 A 重构入口", second["prompt"])
        self.assertIn("用户补充：用方案 A", second["prompt"])

    def test_reply_rejected_when_goal_is_not_waiting_user(self) -> None:
        goal = self._create_goal()
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.reply_goal(None, self.settings, goal["id"], {"message": "hi"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "goal_not_waiting_user")

    # ------------------------------------------------------------------
    # 8. stop cancels the active task
    # ------------------------------------------------------------------

    def test_stop_goal_cancels_active_task(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])

        stopped = goals.stop_goal(None, self.settings, goal["id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["stop_reason"], "用户手动停止了目标")
        task = ags.get_task(self.settings, task_id) or {}
        self.assertEqual(task.get("state"), "cancelled")

        # A stopped goal is never advanced again.
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "stopped")

    # ------------------------------------------------------------------
    # 9. unparsable evaluator output → waiting_user
    # ------------------------------------------------------------------

    def test_unparsable_evaluator_output_waits_for_user(self) -> None:
        self._respond_with("我现在无法判断，总之情况比较复杂……")
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "waiting_user")
        self.assertEqual(detail["question"], goals.EVALUATOR_UNAVAILABLE_QUESTION)
        self.assertEqual(detail["iterations"][0]["decision"], "ask_user")
        self.assertEqual(len(detail["iterations"]), 1)

    def test_continue_without_next_prompt_asks_user(self) -> None:
        self._respond_with({"decision": "continue", "reason": "还要改", "summary": "进行中"})
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "waiting_user")
        self.assertEqual(detail["question"], goals.NO_NEXT_PROMPT_QUESTION)
        self.assertEqual(len(detail["iterations"]), 1)

    # ------------------------------------------------------------------
    # 10. no double evaluation under concurrent ticks
    # ------------------------------------------------------------------

    def test_concurrent_advance_does_not_double_evaluate(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        calls_before = self.fake.calls

        async def _both() -> None:
            await asyncio.gather(
                goals.advance_goals(None, self.settings),
                goals.advance_goals(None, self.settings),
            )

        asyncio.run(_both())

        self.assertEqual(self.fake.calls - calls_before, 1)
        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "achieved")
        self.assertEqual(len(detail["iterations"]), 1)
        self.assertEqual(detail["iterations"][0]["decision"], "achieved")

    # ------------------------------------------------------------------
    # 11. crash safety: missing task row → failed
    # ------------------------------------------------------------------

    def test_missing_task_row_fails_the_goal(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])

        conn = ags._connect(self.settings)
        try:
            conn.execute("DELETE FROM supervisor_tasks WHERE id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "failed")
        self.assertIn("缺失", detail["stop_reason"])

    def test_cancelled_task_stops_the_goal(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "cancelled")

        self._advance()

        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "stopped")
        self.assertIn("取消", detail["stop_reason"])

    def test_evaluation_is_idempotent(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")
        self._advance()
        calls_after_first = self.fake.calls

        # Further ticks must never re-evaluate a decided iteration.
        self._advance()
        self._advance()
        self.assertEqual(self.fake.calls, calls_after_first)
        self.assertEqual(len(self._goal(goal["id"])["iterations"]), 1)

    def test_busy_worktree_defers_next_iteration(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")

        # A foreign task now occupies the same worktree.
        ags.cancel_task(self.settings, task_id, reason="release worktree for foreign task")
        other, _created = svc.create_scoped_task(
            self.settings,
            {
                "goal": "foreign task on the same worktree",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        svc.dispatch_scoped_task(None, self.settings, str(other["id"]), {})

        # The user replies: iteration 2 is recorded but must NOT be dispatched yet.
        replied = goals.reply_goal(None, self.settings, goal["id"], {"message": "继续改"})
        self.assertEqual(replied["state"], "running")
        self.assertFalse(replied["iterations"][1]["task_id"])

        self._advance()
        detail = self._goal(goal["id"])
        self.assertFalse(
            detail["iterations"][1]["task_id"],
            "must never run two tasks on one worktree at once",
        )

        # Once the worktree frees up, the next tick dispatches iteration 2.
        ags.transition(self.settings, str(other["id"]), "failed", stage="failed")
        self._advance()
        detail = self._goal(goal["id"])
        self.assertTrue(detail["iterations"][1]["task_id"])

    # ------------------------------------------------------------------
    # 13. the worker loop never blocks on a goal evaluation
    # ------------------------------------------------------------------

    def test_worker_loop_keeps_claiming_while_goal_evaluator_is_blocked(self) -> None:
        entered = threading.Event()
        release = asyncio.Event()

        async def _blocking_run(
            *,
            role: Any,
            prompt: str,
            worktree_path: str,
            timeout_seconds: int,
            task_id: str = "",
            context: Any = None,
        ) -> ags.ExecutionResult:
            self.fake.calls += 1
            entered.set()
            await release.wait()
            return ags.ExecutionResult(
                success=True,
                output=json.dumps(
                    {"decision": "achieved", "reason": "目标已达成", "summary": "已完成"},
                    ensure_ascii=False,
                ),
                started_at=ags._utc_now(),
                finished_at=ags._utc_now(),
            )

        self.fake.run = _blocking_run

        goal = self._create_goal()
        asyncio.run(goals.advance_goals(None, self.settings))
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        worker = svc.SupervisorWorkerManager(loop)
        claim_calls: list[float] = []
        original_claim = worker._claim_available

        async def _spy_claim(settings: Any) -> None:
            claim_calls.append(time.monotonic())
            await original_claim(settings)

        worker._claim_available = _spy_claim
        worker.ensure_started(self.settings)

        def _cleanup() -> None:
            try:
                worker.stop()
            except Exception:
                pass
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(timeout=3.0)
            except Exception:
                pass
            if not loop.is_closed():
                loop.close()

        self.addCleanup(_cleanup)

        self.assertTrue(entered.wait(timeout=20.0), "evaluator must be entered and block")

        # The worker loop must keep claiming while the evaluator is blocked.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and len(claim_calls) < 3:
            time.sleep(0.05)
        self.assertGreaterEqual(
            len(claim_calls),
            3,
            "worker loop must keep claiming tasks while a goal evaluation is blocked",
        )
        self.assertEqual(self.fake.calls, 1, "goal evaluation must stay single-flight")

        loop.call_soon_threadsafe(release.set)

        settle = time.monotonic() + 20.0
        detail = self._goal(goal["id"])
        while time.monotonic() < settle and detail["state"] == "running":
            time.sleep(0.05)
            detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "achieved")
        self.assertEqual(self.fake.calls, 1)

        # Stop the worker before the temporary state dir is cleaned up in tearDown.
        worker.stop()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    # 12. HTTP routes
    # ------------------------------------------------------------------

    def test_http_goal_routes(self) -> None:
        from web_supervisor_api import handle_get, handle_post

        def _post(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            handler = _FakeHandler(self.settings)
            handle_post(handler, path, path.strip("/").split("/"), body)
            assert handler.response is not None
            return handler.response

        def _get(path: str) -> tuple[int, dict[str, Any]]:
            handler = _FakeHandler(self.settings)
            handle_get(handler, path, path.strip("/").split("/"), {})
            assert handler.response is not None
            return handler.response

        code, created = _post(
            "/api/supervisor/goals",
            {"outcome": OUTCOME, "repo_id": self.repo_id, "worktree_id": self.worktree_id},
        )
        self.assertEqual(code, 201, created)
        goal_id = str(created["id"])

        code, listing = _get("/api/supervisor/goals")
        self.assertEqual(code, 200)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["goals"][0]["id"], goal_id)

        code, detail = _get(f"/api/supervisor/goals/{goal_id}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["state"], "running")

        code, missing = _get("/api/supervisor/goals/goal-does-not-exist")
        self.assertEqual(code, 404)
        self.assertEqual(missing.get("code"), "goal_not_found")

        # Reply is only allowed while waiting for the user.
        code, conflict = _post(f"/api/supervisor/goals/{goal_id}/reply", {"message": "hi"})
        self.assertEqual(code, 409)
        self.assertEqual(conflict.get("code"), "goal_not_waiting_user")

        code, stopped = _post(f"/api/supervisor/goals/{goal_id}/stop", {})
        self.assertEqual(code, 200, stopped)
        self.assertEqual(stopped["state"], "stopped")

        code, missing_stop = _post("/api/supervisor/goals/goal-nope/stop", {})
        self.assertEqual(code, 404)

    def test_http_goal_routes_require_feature_enabled(self) -> None:
        from http import HTTPStatus

        from web_supervisor_api import handle_get, handle_post

        off_settings = dataclasses.replace(
            _make_settings(self.root / "off"),
            agent_supervisor_enabled=False,
        )
        handler = _FakeHandler(off_settings)
        handle_get(handler, "/api/supervisor/goals", ["api", "supervisor", "goals"], {})
        assert handler.response is not None
        self.assertEqual(handler.response[0], HTTPStatus.CONFLICT)
        self.assertEqual(handler.response[1].get("code"), "agent_supervisor_disabled")

        handler_post = _FakeHandler(off_settings)
        handle_post(
            handler_post,
            "/api/supervisor/goals",
            ["api", "supervisor", "goals"],
            {"outcome": OUTCOME},
        )
        assert handler_post.response is not None
        self.assertEqual(handler_post.response[0], HTTPStatus.CONFLICT)


if __name__ == "__main__":
    unittest.main()
