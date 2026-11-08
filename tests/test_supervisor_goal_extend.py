"""tests/test_supervisor_goal_extend.py — 「再试几轮」（extend_goal）unit & HTTP route tests.

Follows tests/test_supervisor_goals.py: disposable state root, real git repo +
managed worktree, real supervisor_service task creation/dispatch, and a fake
coordinator chat executor.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agent_supervisor as ags
import supervisor_goals as goals
import supervisor_service as svc
from config import Settings

OUTCOME = "把构建时间压到 30 秒以内，并且保证所有测试通过"
HAN_RE = re.compile(r"[\u4e00-\u9fff]")


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
        conveyor_web_token="goal-extend-test-token-0123456789-abcdef",
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
    (repo_dir / "README.md").write_text("# Disposable Goal Extend Repo\n", encoding="utf-8")
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


class SupervisorGoalExtendTests(unittest.TestCase):
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
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-extend-")
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
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/goal-extend")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = str(wt["path"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_get_executor(self, _settings: Any, _name: str) -> Any:
        return self.fake

    def _default_responder(self, _prompt: str) -> str:
        return json.dumps(
            {
                "decision": "continue",
                "reason": "构建时间还有 41 秒，还差一点",
                "next_prompt": "并行化打包步骤并开启缓存",
                "question": "",
                "summary": "第 1 轮已结束，构建时间还有 41 秒。",
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

    def _make_exhausted_goal(self, max_iterations: int = 1) -> dict[str, Any]:
        goal = self._create_goal(max_iterations=max_iterations)
        for _ in range(max_iterations):
            self._advance()
            task_id = self._current_task_id(goal["id"])
            self._set_task_state(task_id, "completed")
        self._advance()
        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "exhausted")
        return detail

    def _make_failed_goal(self, *, next_prompt: str = "") -> dict[str, Any]:
        self._respond_with(
            {
                "decision": "give_up",
                "reason": "依赖项冲突尚未解决",
                "next_prompt": next_prompt,
                "question": "",
                "summary": "第 1 轮没有跑成功。",
            }
        )
        goal = self._create_goal(max_iterations=3)
        self._advance()
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "failed")
        self._advance()
        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "failed")
        return detail

    # ------------------------------------------------------------------
    # 1. extend exhausted goal with evaluator's last next_prompt + message
    # ------------------------------------------------------------------

    def test_extend_exhausted_goal_builds_on_last_next_prompt_and_message(self) -> None:
        exhausted = self._make_exhausted_goal(max_iterations=1)
        self.assertEqual(exhausted["last_next_prompt"], "并行化打包步骤并开启缓存")

        extended = goals.extend_goal(
            None,
            self.settings,
            exhausted["id"],
            {"rounds": 3, "message": "顺便跳过重复的类型检查"},
        )
        self.assertEqual(extended["state"], "running")
        self.assertEqual(extended["max_iterations"], 4)
        self.assertEqual(extended["stop_reason"], "")
        self.assertEqual(extended["iterations_used"], 2)
        self.assertEqual(len(extended["iterations"]), 2)

        round2 = extended["iterations"][1]
        self.assertEqual(round2["n"], 2)
        self.assertTrue(round2["task_id"], "extend_goal must start the next round")
        self.assertIn("第 2 轮", round2["prompt"])
        self.assertIn("并行化打包步骤并开启缓存", round2["prompt"])
        self.assertIn("用户补充：顺便跳过重复的类型检查", round2["prompt"])

        task2 = ags.get_task(self.settings, round2["task_id"]) or {}
        self.assertIn(task2.get("state"), ("queued", "dispatching"))

    # ------------------------------------------------------------------
    # 2. extend failed goal without message or with message only
    # ------------------------------------------------------------------

    def test_extend_failed_goal_without_message(self) -> None:
        failed = self._make_failed_goal(next_prompt="先锁定兼容的依赖版本")
        extended = goals.extend_goal(
            None,
            self.settings,
            failed["id"],
            {"rounds": 2},
        )
        self.assertEqual(extended["state"], "running")
        self.assertEqual(extended["max_iterations"], 5)
        self.assertEqual(len(extended["iterations"]), 2)
        round2 = extended["iterations"][1]
        self.assertTrue(round2["task_id"])
        self.assertIn("先锁定兼容的依赖版本", round2["prompt"])
        self.assertNotIn("用户补充：", round2["prompt"])

    def test_extend_failed_goal_with_message_and_no_last_next_prompt(self) -> None:
        failed = self._make_failed_goal(next_prompt="")
        extended = goals.extend_goal(
            None,
            self.settings,
            failed["id"],
            {"rounds": 4, "message": "改用内置模块避免外部依赖"},
        )
        self.assertEqual(extended["state"], "running")
        self.assertEqual(extended["max_iterations"], 7)
        round2 = extended["iterations"][1]
        self.assertTrue(round2["task_id"])
        self.assertIn("用户补充：改用内置模块避免外部依赖", round2["prompt"])

    # ------------------------------------------------------------------
    # 3. hard rules still hold after extending
    # ------------------------------------------------------------------

    def test_extend_preserves_hard_rules_and_new_iteration_budget(self) -> None:
        exhausted = self._make_exhausted_goal(max_iterations=1)
        extended = goals.extend_goal(
            None,
            self.settings,
            exhausted["id"],
            {"rounds": 2},
        )
        self.assertEqual(extended["max_iterations"], 3)
        task2_id = self._current_task_id(exhausted["id"])

        # Evaluator claims achieved on a failed task -> downgraded to continue (round 3).
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "看起来差不多了",
                "next_prompt": "修复构建脚本的退出码",
                "question": "",
                "summary": "第 2 轮失败但继续修复。",
            }
        )
        self._set_task_state(task2_id, "failed")
        self._advance()

        after_round2 = self._goal(exhausted["id"])
        self.assertEqual(after_round2["state"], "running")
        self.assertEqual(after_round2["iterations"][1]["decision"], "continue")
        self.assertEqual(len(after_round2["iterations"]), 3)

        # Round 3 completes and evaluator says achieved -> goal reaches achieved.
        task3_id = self._current_task_id(exhausted["id"])
        self._respond_with(
            {
                "decision": "achieved",
                "reason": "构建已降到 22 秒且测试通过",
                "next_prompt": "",
                "question": "",
                "summary": "第 3 轮已达成目标。",
            }
        )
        self._set_task_state(task3_id, "completed")
        self._advance()

        final = self._goal(exhausted["id"])
        self.assertEqual(final["state"], "achieved")
        self.assertEqual(final["iterations"][2]["decision"], "achieved")

    # ------------------------------------------------------------------
    # 4. 409 when goal state is not exhausted/failed or worktree is busy
    # ------------------------------------------------------------------

    def test_extend_rejected_with_409_chinese_message_for_non_extendable_states(self) -> None:
        # 1) running
        goal = self._create_goal()
        self._advance()
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.extend_goal(None, self.settings, goal["id"], {"rounds": 3})
        self.assertEqual(ctx.exception.status, 409)
        self.assertRegex(ctx.exception.message, HAN_RE)

        # 2) waiting_user
        task_id = self._current_task_id(goal["id"])
        self._set_task_state(task_id, "waiting_user", stage="needs_validation")
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "waiting_user")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.extend_goal(None, self.settings, goal["id"], {"rounds": 3})
        self.assertEqual(ctx.exception.status, 409)
        self.assertRegex(ctx.exception.message, HAN_RE)

        # 3) achieved
        goals.reply_goal(None, self.settings, goal["id"], {"achieved": True})
        self.assertEqual(self._goal(goal["id"])["state"], "achieved")
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.extend_goal(None, self.settings, goal["id"], {"rounds": 3})
        self.assertEqual(ctx.exception.status, 409)
        self.assertRegex(ctx.exception.message, HAN_RE)

        # 4) stopped
        goal2 = self._create_goal()
        goals.stop_goal(None, self.settings, goal2["id"])
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.extend_goal(None, self.settings, goal2["id"], {"rounds": 3})
        self.assertEqual(ctx.exception.status, 409)
        self.assertRegex(ctx.exception.message, HAN_RE)

    def test_extend_rejected_with_409_when_worktree_has_active_task(self) -> None:
        exhausted = self._make_exhausted_goal(max_iterations=1)

        # Start another task on the same worktree.
        foreign, _created = svc.create_scoped_task(
            self.settings,
            {
                "goal": "同工作目录上的其他任务",
                "repo_id": self.repo_id,
                "worktree_id": self.worktree_id,
            },
        )
        svc.dispatch_scoped_task(None, self.settings, str(foreign["id"]), {})

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.extend_goal(None, self.settings, exhausted["id"], {"rounds": 3})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "worktree_busy")
        self.assertRegex(ctx.exception.message, HAN_RE)

        unchanged = self._goal(exhausted["id"])
        self.assertEqual(unchanged["state"], "exhausted")
        self.assertEqual(unchanged["max_iterations"], 1)

    # ------------------------------------------------------------------
    # 5. input validation (rounds 1-10, message <= 2000)
    # ------------------------------------------------------------------

    def test_extend_validates_rounds_and_message(self) -> None:
        exhausted = self._make_exhausted_goal(max_iterations=1)
        for bad_rounds in (None, 0, -2, 11, 99, "3", True, False, 2.5):
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                goals.extend_goal(
                    None,
                    self.settings,
                    exhausted["id"],
                    {"rounds": bad_rounds},
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "invalid_rounds")
            self.assertRegex(ctx.exception.message, HAN_RE)

        for bad_msg in (123, ["x"], "补充" * 1001, "坏字符\x00测试"):
            with self.assertRaises(svc.SupervisorServiceError) as ctx:
                goals.extend_goal(
                    None,
                    self.settings,
                    exhausted["id"],
                    {"rounds": 3, "message": bad_msg},
                )
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "invalid_message")
            self.assertRegex(ctx.exception.message, HAN_RE)

        # Boundary: rounds=10 and 2000-char message succeed.
        ok = goals.extend_goal(
            None,
            self.settings,
            exhausted["id"],
            {"rounds": 10, "message": "补" * 2000},
        )
        self.assertEqual(ok["state"], "running")
        self.assertEqual(ok["max_iterations"], 11)

    # ------------------------------------------------------------------
    # 6. HTTP route POST /api/supervisor/goals/<id>/extend
    # ------------------------------------------------------------------

    def test_http_extend_goal_route(self) -> None:
        from web_supervisor_api import handle_post

        def _post(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            handler = _FakeHandler(self.settings)
            handle_post(handler, path, path.strip("/").split("/"), body)
            assert handler.response is not None
            return handler.response

        exhausted = self._make_exhausted_goal(max_iterations=2)
        goal_id = exhausted["id"]

        # Invalid rounds -> 400 with Chinese error
        code, bad = _post(f"/api/supervisor/goals/{goal_id}/extend", {"rounds": 0})
        self.assertEqual(code, 400, bad)
        self.assertRegex(str(bad.get("error") or ""), HAN_RE)

        # Valid extend -> 200, running, max_iterations raised by 3, round 3 started
        code, extended = _post(
            f"/api/supervisor/goals/{goal_id}/extend",
            {"rounds": 3, "message": "从 HTTP 接口补充说明"},
        )
        self.assertEqual(code, 200, extended)
        self.assertEqual(extended["state"], "running")
        self.assertEqual(extended["max_iterations"], 5)
        self.assertEqual(extended["iterations_used"], 3)
        self.assertIn("用户补充：从 HTTP 接口补充说明", extended["iterations"][2]["prompt"])

        # Extending again while running -> 409 with Chinese error
        code, conflict = _post(f"/api/supervisor/goals/{goal_id}/extend", {"rounds": 2})
        self.assertEqual(code, 409, conflict)
        self.assertRegex(str(conflict.get("error") or ""), HAN_RE)

        # Missing goal -> 404
        code, missing = _post("/api/supervisor/goals/goal-does-not-exist/extend", {"rounds": 3})
        self.assertEqual(code, 404, missing)
        self.assertEqual(missing.get("code"), "goal_not_found")


if __name__ == "__main__":
    unittest.main()
