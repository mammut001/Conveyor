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

from conveyor.agents import agent_memory
from conveyor.agents import agent_supervisor as ags
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
from conveyor.core.config import Settings

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
        from conveyor.web.web_supervisor_api import handle_get, handle_post

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

        from conveyor.web.web_supervisor_api import handle_get, handle_post

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

    # ------------------------------------------------------------------
    # 14. S2 robustness & hardening tests
    # ------------------------------------------------------------------

    def test_advance_one_goal_offloads_sync_steps_to_thread_and_keeps_evaluator_async(self) -> None:
        from unittest.mock import patch

        goal = self._create_goal()
        to_thread_funcs: list[str] = []
        orig_to_thread = asyncio.to_thread
        eval_thread_id: list[int] = []
        loop_thread_id: list[int] = []

        async def _spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
            to_thread_funcs.append(getattr(func, "__name__", str(func)))
            return await orig_to_thread(func, *args, **kwargs)

        orig_run = self.fake.run

        async def _spy_eval_run(**kwargs: Any) -> ags.ExecutionResult:
            eval_thread_id.append(threading.get_ident())
            return await orig_run(**kwargs)

        self.fake.run = _spy_eval_run
        self._respond_with(
            {"decision": "achieved", "reason": "全部完成", "summary": "已完成"}
        )

        async def _drive() -> None:
            loop_thread_id.append(threading.get_ident())
            with patch("conveyor.agents.supervisor_goals.asyncio.to_thread", side_effect=_spy_to_thread):
                await goals.advance_goals(None, self.settings)
                task_id = self._current_task_id(goal["id"])
                self._set_task_state(task_id, "completed")
                await goals.advance_goals(None, self.settings)

        asyncio.run(_drive())

        self.assertIn("_running_goals", to_thread_funcs)
        self.assertIn("_get_goal_row", to_thread_funcs)
        self.assertIn("_iterations", to_thread_funcs)
        self.assertIn("_start_iteration", to_thread_funcs)
        self.assertIn("get_task", to_thread_funcs)
        self.assertIn("_claim_iteration", to_thread_funcs)
        self.assertIn("_scope_paths", to_thread_funcs)
        self.assertIn("_prepare_evaluator_call", to_thread_funcs)
        self.assertIn("_apply_evaluation_decision", to_thread_funcs)
        # Evaluator call itself is natively async on the event loop thread.
        self.assertNotIn("_invoke_role_executor", to_thread_funcs)
        self.assertEqual(eval_thread_id, loop_thread_id)
        self.assertEqual(self._goal(goal["id"])["state"], "achieved")

    def test_failure_after_record_decision_recovers_on_next_tick_achieved(self) -> None:
        from unittest.mock import patch

        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._respond_with(
            {"decision": "achieved", "reason": "验收全部通过", "summary": "第 1 轮完成目标"}
        )

        orig_record = goals._record_decision

        def _record_then_crash(*args: Any, **kwargs: Any) -> None:
            orig_record(*args, **kwargs)
            raise RuntimeError("injected crash right after _record_decision")

        with patch("conveyor.agents.supervisor_goals._record_decision", side_effect=_record_then_crash):
            self._advance()

        # After the crash: iteration has decision='achieved' committed, goal is still running.
        mid = self._goal(goal["id"])
        self.assertEqual(mid["state"], "running")
        self.assertEqual(mid["iterations"][0]["decision"], "achieved")
        self.assertEqual(self.fake.calls, 1)

        # Next tick must re-apply the stored decision idempotently without re-running evaluator.
        self._advance()
        final = self._goal(goal["id"])
        self.assertEqual(final["state"], "achieved")
        self.assertEqual(final["stop_reason"], "验收全部通过")
        self.assertIn("第 1 轮完成目标", final["summary"])
        self.assertEqual(self.fake.calls, 1)

    def test_failure_after_record_decision_recovers_on_next_tick_continue(self) -> None:
        from unittest.mock import patch

        goal = self._create_goal(max_iterations=3)
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        self._respond_with(
            {
                "decision": "continue",
                "reason": "还需补齐边界测试",
                "next_prompt": "请补充边界条件测试",
                "summary": "第 1 轮已完成主流程",
            }
        )

        orig_record = goals._record_decision

        def _record_then_crash(*args: Any, **kwargs: Any) -> None:
            orig_record(*args, **kwargs)
            raise RuntimeError("injected crash right after _record_decision")

        with patch("conveyor.agents.supervisor_goals._record_decision", side_effect=_record_then_crash):
            self._advance()

        mid = self._goal(goal["id"])
        self.assertEqual(mid["state"], "running")
        self.assertEqual(len(mid["iterations"]), 1)
        self.assertEqual(mid["iterations"][0]["decision"], "continue")
        self.assertEqual(self.fake.calls, 1)

        # Next tick re-applies the stored continue decision and starts iteration 2.
        self._advance()
        after = self._goal(goal["id"])
        self.assertEqual(after["state"], "running")
        self.assertEqual(len(after["iterations"]), 2)
        self.assertTrue(after["iterations"][1]["task_id"])
        self.assertIn("请补充边界条件测试", after["iterations"][1]["prompt"])
        self.assertEqual(self.fake.calls, 1)

    def test_stop_goal_interleaving_between_iteration_insert_and_dispatch_cancels_task(self) -> None:
        from unittest.mock import patch

        goal = self._create_goal()
        orig_create_scoped = svc.create_scoped_task
        created_task_ids: list[str] = []

        def _create_while_stop_interleaves(settings: Any, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            # Simulate HTTP stop_goal running between _ensure_iteration (task_id='') and task creation/dispatch.
            goals.stop_goal(None, settings, goal["id"])
            task, created = orig_create_scoped(settings, body)
            created_task_ids.append(str(task["id"]))
            return task, created

        with patch("conveyor.agents.supervisor_goals.svc.create_scoped_task", side_effect=_create_while_stop_interleaves):
            # Force _start_iteration past the initial _goal_is_running check so stop_goal happens
            # right between _ensure_iteration and create_scoped_task / dispatch_scoped_task.
            goals._ensure_iteration(self.settings, goal["id"], 1, goals._first_iteration_prompt(goal))
            orig_is_running = goals._goal_is_running
            check_count = 0

            def _is_running_first_true(settings: Any, gid: str) -> bool:
                nonlocal check_count
                check_count += 1
                if check_count <= 2:
                    return True
                return orig_is_running(settings, gid)

            with patch("conveyor.agents.supervisor_goals._goal_is_running", side_effect=_is_running_first_true):
                ret = goals._start_iteration(
                    None, self.settings, goal, 1, goals._first_iteration_prompt(goal)
                )

        self.assertEqual(ret, "")
        self.assertEqual(len(created_task_ids), 1)
        task = ags.get_task(self.settings, created_task_ids[0])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(self._goal(goal["id"])["state"], "stopped")

    def test_stop_goal_interleaving_during_dispatch_cancels_dispatched_task(self) -> None:
        from unittest.mock import patch

        goal = self._create_goal()
        orig_dispatch = svc.dispatch_scoped_task
        dispatched_task_ids: list[str] = []

        def _dispatch_then_stop(
            server: Any, settings: Any, task_id: str, body: dict[str, Any]
        ) -> dict[str, Any]:
            res = orig_dispatch(server, settings, task_id, body)
            dispatched_task_ids.append(task_id)
            # Simulate stop_goal racing right as dispatch_scoped_task finishes,
            # or having marked the goal stopped before task_id was visible.
            goals._update_goal(
                settings,
                goal["id"],
                state=goals.GOAL_STOPPED,
                stop_reason="用户手动停止了目标",
                updated_at=goals._utc_now(),
            )
            return res

        with patch("conveyor.agents.supervisor_goals.svc.dispatch_scoped_task", side_effect=_dispatch_then_stop):
            self._advance()

        self.assertEqual(len(dispatched_task_ids), 1)
        task = ags.get_task(self.settings, dispatched_task_ids[0])
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task["state"], "cancelled")
        self.assertEqual(self._goal(goal["id"])["state"], "stopped")

    def test_reply_goal_budget_exhausted_does_not_cancel_waiting_task(self) -> None:
        from http import HTTPStatus

        goal = self._create_goal(max_iterations=1)
        self._advance()
        t1 = self._current_task_id(goal["id"])
        self._set_task_state(t1, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")

        # Iteration 2 is allowed (max_iterations + 1 == 2).
        replied = goals.reply_goal(None, self.settings, goal["id"], {"message": "第一次补充"})
        self.assertEqual(replied["state"], "running")
        self._advance()
        t2 = self._current_task_id(goal["id"])
        self.assertTrue(t2)
        self._set_task_state(t2, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")

        # Now next_n == 3 > max_iterations + 1 (2): reply must fail with 409 BEFORE cancelling t2.
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.reply_goal(None, self.settings, goal["id"], {"message": "超出预算的回复"})
        self.assertEqual(ctx.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(ctx.exception.code, "goal_iterations_exhausted")

        t2_after = ags.get_task(self.settings, t2)
        self.assertIsNotNone(t2_after)
        assert t2_after is not None
        self.assertEqual(
            t2_after["state"],
            "waiting_user",
            "waiting task must not be cancelled when reply is rejected for exhausted budget",
        )

    def test_reply_goal_concurrent_replies_do_not_500_and_return_409(self) -> None:
        from http import HTTPStatus
        from unittest.mock import patch

        goal = self._create_goal(max_iterations=3)
        self._advance()
        t1 = self._current_task_id(goal["id"])
        self._set_task_state(t1, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")

        # Verify both lock contention and conditional state transition return 409
        # 「已经在处理你的回复」 instead of raising sqlite3.IntegrityError (500).
        results: list[tuple[str, Any]] = []
        barrier = threading.Barrier(2)

        def _worker(msg: str) -> None:
            barrier.wait(timeout=5.0)
            try:
                res = goals.reply_goal(None, self.settings, goal["id"], {"message": msg})
                results.append(("ok", res))
            except svc.SupervisorServiceError as exc:
                results.append(("err", exc))
            except Exception as exc:  # pragma: no cover
                results.append(("unexpected", exc))

        th1 = threading.Thread(target=_worker, args=("回复 A",))
        th2 = threading.Thread(target=_worker, args=("回复 B",))
        th1.start()
        th2.start()
        th1.join(timeout=10.0)
        th2.join(timeout=10.0)

        self.assertEqual(len(results), 2)
        oks = [r for kind, r in results if kind == "ok"]
        errs = [r for kind, r in results if kind == "err"]
        self.assertEqual(len(oks), 1, f"expected 1 success, got {results}")
        self.assertEqual(len(errs), 1, f"expected 1 conflict error, got {results}")
        self.assertEqual(errs[0].status, HTTPStatus.CONFLICT)
        self.assertEqual(errs[0].message, "已经在处理你的回复")
        self.assertEqual(errs[0].code, "goal_reply_in_progress")

        # Stop the first goal to free the worktree before testing the conditional UPDATE race.
        goals.stop_goal(None, self.settings, goal["id"])

        goal2 = self._create_goal(max_iterations=3)
        self._advance()
        t2 = self._current_task_id(goal2["id"])
        self._set_task_state(t2, "waiting_user", stage="needs_validation")
        self._advance()

        orig_get_row = goals._get_goal_row

        def _stale_waiting_row(settings: Any, gid: str) -> dict[str, Any]:
            row = orig_get_row(settings, gid)
            # Simulate second request reading stale waiting_user before acquiring DB write,
            # while the DB row has already been updated to running by the first reply.
            conn = goals._connect(settings)
            try:
                conn.execute(
                    "UPDATE supervisor_goals SET state = ? WHERE id = ?",
                    (goals.GOAL_RUNNING, gid),
                )
                conn.commit()
            finally:
                conn.close()
            return row

        with patch("conveyor.agents.supervisor_goals._get_goal_row", side_effect=_stale_waiting_row):
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                goals.reply_goal(None, self.settings, goal2["id"], {"message": "并发第二次回复"})
        self.assertEqual(ctx.exception.status, HTTPStatus.CONFLICT)
        self.assertEqual(ctx.exception.message, "已经在处理你的回复")
        self.assertEqual(ctx.exception.code, "goal_reply_in_progress")

    def test_pending_dispatch_treated_as_inflight_and_redispatched_on_next_tick(self) -> None:
        goal = self._create_goal()
        prompt = goals._first_iteration_prompt(goal)
        goals._ensure_iteration(self.settings, goal["id"], 1, prompt)
        task, _created = svc.create_scoped_task(
            self.settings,
            {
                "goal": prompt,
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        task_id = str(task["id"])
        goals._set_iteration_task(self.settings, goal["id"], 1, task_id)

        # Sanity check: task is in pending_dispatch (created before crash, never dispatched).
        raw_task = ags.get_task(self.settings, task_id)
        self.assertIsNotNone(raw_task)
        assert raw_task is not None
        self.assertEqual(raw_task["state"], "pending_dispatch")

        # Next tick must NOT evaluate the pending_dispatch task as finished;
        # it must re-dispatch it instead.
        self._advance()
        self.assertEqual(self.fake.calls, 0, "pending_dispatch task must not be evaluated as finished")
        after_task = ags.get_task(self.settings, task_id)
        self.assertIsNotNone(after_task)
        assert after_task is not None
        self.assertNotEqual(after_task["state"], "pending_dispatch")
        self.assertIn(after_task["state"], goals._GOAL_INFLIGHT_STATES)
        self.assertEqual(self._goal(goal["id"])["state"], "running")

    def test_evaluator_failure_log_redacts_and_truncates_error(self) -> None:
        goal = self._create_goal()
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "completed")

        secret = "sk-proj-supersecretkey1234567890abcdef"
        long_tail = "X" * (goals.MAX_REASON_CHARS + 500)

        async def _failing_run(**_kwargs: Any) -> ags.ExecutionResult:
            self.fake.calls += 1
            return ags.ExecutionResult(
                success=False,
                output="",
                error=f"Authorization: Bearer {secret} {long_tail}",
                started_at=ags._utc_now(),
                finished_at=ags._utc_now(),
            )

        self.fake.run = _failing_run

        with self.assertLogs(goals.log, level="WARNING") as cm:
            self._advance()

        joined = "\n".join(cm.output)
        self.assertNotIn(secret, joined)
        self.assertIn("[REDACTED]", joined)
        self.assertIn("evaluator returned failure:", joined)
        for line in cm.output:
            if "evaluator returned failure:" in line:
                suffix = line.split("evaluator returned failure:", 1)[1].strip()
                self.assertLessEqual(len(suffix), goals.MAX_REASON_CHARS)

    # ------------------------------------------------------------------
    # Goal lessons -> agent_memory (scope repo:<repo_id>, source untrusted)
    # ------------------------------------------------------------------

    def test_lessons_stored_only_when_goal_achieved_with_untrusted_source(self) -> None:
        scope = f"repo:{self.repo_id}"
        goal = self._create_goal(max_iterations=4)

        # Round 1: decision=continue with lessons -> must NOT store notes.
        self._advance()
        t1 = self._current_task_id(goal["id"])
        self._set_task_state(t1, "completed")
        self._respond_with(
            {
                "decision": "continue",
                "reason": "还要补测试",
                "next_prompt": "补齐单元测试",
                "question": "",
                "summary": "第 1 轮完成代码，还需补测试。",
                "lessons": ["继续轮次里的经验不该提前落库"],
            }
        )
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "running")
        self.assertEqual(agent_memory.list_notes(self.settings, scope), [])

        # Round 2: task failed, evaluator claims achieved -> downgraded to continue, must NOT store notes.
        t2 = self._current_task_id(goal["id"])
        self._set_task_state(t2, "failed")
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "误判达成",
                "next_prompt": "修复失败的任务",
                "question": "",
                "summary": "第 2 轮失败。",
                "lessons": ["降级轮次里的经验也不该落库"],
            }
        )
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "running")
        self.assertEqual(agent_memory.list_notes(self.settings, scope), [])

        # Round 3: task completed, decision=achieved -> lessons ARE stored as untrusted.
        t3 = self._current_task_id(goal["id"])
        self._set_task_state(t3, "completed")
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "全部完成",
                "next_prompt": "",
                "question": "",
                "summary": "第 3 轮已达成。",
                "lessons": [
                    "后端单测使用 python3 -m unittest discover -s tests 运行",
                    "所有面向用户的提示文案必须使用简体中文",
                    "不要直接修改主工作区，改动都在托管 worktree 里完成",
                ],
            }
        )
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "achieved")
        notes = agent_memory.list_notes(self.settings, scope)
        self.assertEqual(
            [n["text"] for n in notes],
            [
                "后端单测使用 python3 -m unittest discover -s tests 运行",
                "所有面向用户的提示文案必须使用简体中文",
                "不要直接修改主工作区，改动都在托管 worktree 里完成",
            ],
        )
        for note in notes:
            self.assertEqual(note["scope"], scope)
            self.assertEqual(note["source"], "untrusted")

    def test_lessons_not_stored_on_give_up_or_ask_user(self) -> None:
        scope = f"repo:{self.repo_id}"
        goal = self._create_goal(max_iterations=3)
        self._advance()
        t1 = self._current_task_id(goal["id"])
        self._set_task_state(t1, "completed")
        self._respond_with(
            {
                "decision": "ask_user",
                "reason": "需要确认",
                "next_prompt": "",
                "question": "是否保留旧接口？",
                "summary": "等待确认。",
                "lessons": ["ask_user 时不落库"],
            }
        )
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")
        self.assertEqual(agent_memory.list_notes(self.settings, scope), [])

        goals.reply_goal(None, self.settings, goal["id"], {"message": "不用保留旧接口"})
        self._advance()
        t2 = self._current_task_id(goal["id"])
        self._set_task_state(t2, "completed")
        self._respond_with(
            {
                "decision": "give_up",
                "reason": "外部依赖缺失",
                "next_prompt": "",
                "question": "",
                "summary": "放弃目标。",
                "lessons": ["give_up 时也不落库"],
            }
        )
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "failed")
        self.assertEqual(agent_memory.list_notes(self.settings, scope), [])

    def test_lessons_caps_duplicates_and_secrets_ignored_safely(self) -> None:
        scope = f"repo:{self.repo_id}"
        agent_memory.add_note(self.settings, scope, "已有的仓库约定", source="owner")

        goal = self._create_goal()
        self._advance()
        t1 = self._current_task_id(goal["id"])
        self._set_task_state(t1, "completed")

        secret_lesson = "测试 token 是 sk-abcdefghijklmnopqrstuvwxyz012345"
        overlong_lesson = "长" * 201
        valid_lesson = "每次改动后跑 pytest -q"
        extra_4th_lesson = "第四条超出每轮 3 条上限不该写入"

        self._respond_with(
            {
                "decision": "achieved",
                "reason": "已完成",
                "next_prompt": "",
                "question": "",
                "summary": "达成。",
                "lessons": [
                    secret_lesson,
                    overlong_lesson,
                    valid_lesson,
                    extra_4th_lesson,
                ],
            }
        )
        with self.assertLogs(goals.log, level="WARNING") as cm:
            self._advance()

        self.assertEqual(self._goal(goal["id"])["state"], "achieved")
        joined_logs = "\n".join(cm.output)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", joined_logs)

        notes = agent_memory.list_notes(self.settings, scope)
        self.assertEqual([n["text"] for n in notes], ["已有的仓库约定", valid_lesson])
        self.assertEqual(notes[0]["source"], "owner")
        self.assertEqual(notes[1]["source"], "untrusted")

        # Now fill the repo scope to 2200 chars and verify notes_full is ignored safely
        # and duplicate lessons do not fail the goal.
        for n in list(agent_memory.list_notes(self.settings, scope)):
            agent_memory.remove_note(self.settings, n["id"])
        for idx in range(4):
            agent_memory.add_note(self.settings, scope, str(idx) * 500, source="owner")
        agent_memory.add_note(self.settings, scope, "z" * 200, source="owner")
        self.assertEqual(agent_memory.used_chars(self.settings, scope), 2200)

        goal2 = self._create_goal()
        self._advance()
        t2 = self._current_task_id(goal2["id"])
        self._set_task_state(t2, "completed")
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "容量满时仍达成",
                "next_prompt": "",
                "question": "",
                "summary": "达成。",
                "lessons": ["容量已满时新增的经验", "z" * 200],
            }
        )
        with self.assertLogs(goals.log, level="WARNING") as cm2:
            self._advance()
        self.assertEqual(self._goal(goal2["id"])["state"], "achieved")
        self.assertTrue(any("notes_full" in line for line in cm2.output))
        self.assertEqual(agent_memory.used_chars(self.settings, scope), 2200)

    def test_evaluator_without_lessons_or_with_non_list_lessons_still_works(self) -> None:
        scope = f"repo:{self.repo_id}"
        for idx, lessons_val in enumerate((None, "not-a-list", [None, 123, "   ", "单条有效经验", "单条有效经验"])):
            goal = self._create_goal()
            self._advance()
            tid = self._current_task_id(goal["id"])
            self._set_task_state(tid, "completed")
            payload: dict[str, Any] = {
                "decision": "achieved",
                "reason": f"完成 {idx}",
                "next_prompt": "",
                "question": "",
                "summary": f"达成 {idx}",
            }
            if lessons_val is not None:
                payload["lessons"] = lessons_val
            self._respond_with(payload)
            self._advance()
            self.assertEqual(self._goal(goal["id"])["state"], "achieved")

        notes = agent_memory.list_notes(self.settings, scope)
        self.assertEqual([n["text"] for n in notes], ["单条有效经验"])
        self.assertEqual(notes[0]["source"], "untrusted")


if __name__ == "__main__":
    unittest.main()
