"""tests/test_supervisor_goals_timeboxed.py — Time-boxed & continuous goals tests (Task A1).

Covers: duration validation, deadline enforcement (never start after deadline,
let running round finish), continuous mode prompts + round tracking, 3 consecutive
failures pause, quota pause/auto-resume/deadline-exceeded stop, the boundary of
what counts as a quota failure (a provider's own quota message does, a repo
check's output does not), startup recovery, goal dict metrics, and schema
migration from older DBs.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import agent_supervisor_pool as worker_pool
from conveyor.agents import agents
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
from conveyor.core.config import load_runtime_settings
from conveyor.core.transcript_store import get_transcript_store
from conveyor.web import web_push
from conveyor.web.web_chat import build_history
from tests.test_supervisor_goals import (
    OUTCOME,
    FakeChatExecutor,
    _init_repo,
    _make_settings,
)


class _Clock:
    """Mutable deterministic UTC clock for patching `supervisor_goals._now`."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: Any) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


# A repo check that really failed. Its output quotes the worktree path, whose
# name embeds the task id (`sup-<ms-timestamp>-<hex>`), so it can contain the
# bare digits `429` (and timestamps contain them often) without anything being
# wrong with the model's quota. This is what CI once fed to
# `supervisor_goals._is_quota_failure` (tests/test_goal_live_script.py).
CHECK_OUTPUT_WITH_429 = (
    "EEE\n"
    "======================================================================\n"
    "ERROR: test_add_integers (test_calc.AddTests.test_add_integers)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/tmp/conveyor-goal-live-9x2q7faa/live/tasks/worktrees/supervisor/'
    'sup-1794947997429-3ac91b77/tests/test_calc.py", line 7, in test_add_integers\n'
    "    self.assertEqual(calc.add(2, 3), 5)\n"
    "NotImplementedError: add 还没实现\n"
    "----------------------------------------------------------------------\n"
    "Ran 6 tests in 0.002s\n"
    "\n"
    "FAILED (errors=6)\n"
)
# What a model executor itself reports when its provider is out of quota.
PROVIDER_QUOTA_ERROR = "Claude AI usage limit reached|1794947997"


class _TimeboxedGoalBase(unittest.TestCase):
    def _make_test_settings(self) -> Any:
        return _make_settings(self.root)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-timebox-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = self._make_test_settings()
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        self.clock = _Clock(datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc))
        clock_patch = patch.object(goals, "_now", side_effect=self.clock.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

        self.responder = self._default_responder
        self.fake = FakeChatExecutor(self.settings, lambda prompt: self.responder(prompt))
        executor_patch = patch.object(ags, "get_executor", lambda _s, _n: self.fake)
        executor_patch.start()
        self.addCleanup(executor_patch.stop)

        for module in (goals, goal_notify):
            notify_patch = patch.object(module, "notify_goal_change", lambda *_a, **_kw: None)
            notify_patch.start()
            self.addCleanup(notify_patch.stop)

        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/timeboxed")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])
        self.wt_path = str(wt["path"])

    def tearDown(self) -> None:
        goal_notify.drain()
        self._tmp.cleanup()

    def _default_responder(self, _prompt: str) -> str:
        return json.dumps(
            {
                "decision": "achieved",
                "reason": "本轮改进已完成",
                "next_prompt": "",
                "question": "",
                "summary": "已完成本轮改进。",
                "round_title": "完成基础优化",
            },
            ensure_ascii=False,
        )

    def _create(self, **overrides: Any) -> dict[str, Any]:
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

    def _set_task_state(self, task_id: str, state: str, *, error: str = "") -> None:
        if state == "completed":
            identity = ags.target_identity(self.wt_path)
            ags.record_acceptance(self.settings, task_id, sha=identity["sha"], passed=True,
                                  fingerprint=identity["fingerprint"],
                                  evidence_source="operator_validation+independent_review",
                                  executed_checks=[{"passed": True, "execution_id": "test-check",
                                                    "target_sha": identity["sha"],
                                                    "target_fingerprint": identity["fingerprint"]}],
                                  provenance_verified=True)
            ags.transition(self.settings, task_id, "completed", stage="completed")
            return
        ags.transition(self.settings, task_id, state, stage=state, error=error or None)

    def _seed_failed_check_round(self, task_id: str, check_output: str) -> None:
        """Leave behind what a failed repo check leaves: the check's own execution
        row (its error is the raw command output) and the acceptance issue that
        quotes it."""
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, status, started_at, error)
                       VALUES (?,?,?,?,?,?,?)""",
                    ("ex-check01", task_id, "validator", "operator-validation", "failed",
                     "2026-11-15T10:00:00+00:00", check_output),
                )
                conn.execute(
                    """INSERT INTO supervisor_acceptance
                       (id, task_id, sha, passed, issues_json, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    ("acc-check01", task_id, "0" * 40, 0,
                     json.dumps(
                         [f"operator validation failed: unittest (exit 1): {check_output[:240]}"],
                         ensure_ascii=False,
                     ),
                     "2026-11-15T10:00:00+00:00"),
                )
        finally:
            conn.close()



class TimeboxedGoalTests(_TimeboxedGoalBase):
    def test_evaluator_retry_garbage_then_valid(self) -> None:
        responses = [
            "garbage1",
            json.dumps({"decision": "continue", "reason": "valid", "next_prompt": "ok", "summary": "ok", "round_title": "ok"})
        ]
        
        def responder(prompt: str) -> str:
            return responses.pop(0)

        self.responder = responder
        goal = self._create()
        self._advance()
        t1 = self._goal(goal["id"])["iterations"][0]["task_id"]
        self._set_task_state(t1, "completed")
        self._advance()
        
        detail = self._goal(goal["id"])
        self.assertEqual(detail["state"], "running")
        self.assertEqual(detail["iterations"][0]["decision"], "continue")
        self.assertEqual(len(detail["iterations"]), 2)
        self.assertEqual(self.fake.calls, 2)

    def test_continuous_goal_three_garbage_replies_continues(self) -> None:
        goal = self._create(mode="continuous")
        gid = goal["id"]
        
        # 3 garbage replies: 1st initial, 2nd, 3rd (retries)
        self.responder = lambda _p: "garbage"
        
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._set_task_state(t1, "completed")
        self._advance()
        
        detail = self._goal(gid)
        self.assertEqual(detail["state"], "running")
        self.assertEqual(detail["iterations"][0]["decision"], "continue")
        self.assertEqual(len(detail["iterations"]), 2)
        # Should NOT be counted in consecutive_failures
        self.assertEqual(detail["consecutive_failures"], 0)

    def test_continuous_goal_three_garbage_replies_failed_task_continues_failed(self) -> None:
        goal = self._create(mode="continuous")
        gid = goal["id"]
        
        self.responder = lambda _p: "garbage"
        
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._set_task_state(t1, "failed", error="test failure")
        self._advance()
        
        detail = self._goal(gid)
        self.assertEqual(detail["state"], "running")
        self.assertEqual(detail["iterations"][0]["decision"], "continue")
        self.assertEqual(len(detail["iterations"]), 2)
        # Failure itself is counted, but not the unparseable evaluator
        self.assertEqual(detail["consecutive_failures"], 0)

    def test_retry_prompt_contains_previous_reply(self) -> None:
        garbage = "this is garbage"
        calls = []
        
        def responder(prompt: str) -> str:
            calls.append(prompt)
            # The initial call is "garbage"
            # The retries are empty in the prompt because `_ask_evaluator` might be stripping it?
            return garbage

        self.responder = responder
        goal = self._create()
        self._advance()
        t1 = self._goal(goal["id"])["iterations"][0]["task_id"]
        self._set_task_state(t1, "completed")
        self._advance()
        
        self.assertEqual(len(calls), 3) # initial + 2 retries
        self.assertIn("Previous reply", calls[1])

    def test_duration_validation_and_goal_dict_fields(self) -> None:
        for bad in (29, 1441, 0, -60, "abc", True, 45.5):
            with self.subTest(bad=bad):
                with self.assertRaises(svc.SupervisorServiceError) as ctx:
                    self._create(duration_minutes=bad)
                self.assertEqual(ctx.exception.status, 400)
                self.assertEqual(ctx.exception.code, "invalid_duration")
                self.assertRegex(ctx.exception.message, r"[\u4e00-\u9fff]")

        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            self._create(mode="forever")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_mode")

        # Time-boxed goal ignores user's max_iterations and clamps to MAX_TIMEBOXED_ITERATIONS.
        tb = self._create(duration_minutes=600, max_iterations=3, mode="continuous")
        self.assertEqual(tb["max_iterations"], goals.MAX_TIMEBOXED_ITERATIONS)
        self.assertEqual(tb["mode"], "continuous")
        self.assertEqual(tb["deadline_at"], "2026-11-15T20:00:00+00:00")
        self.assertIsNone(tb["resume_at"])
        self.assertEqual(tb["rounds_passed"], 0)
        self.assertEqual(tb["rounds_total"], 0)
        self.assertEqual(tb["remaining_seconds"], 36000)

        self.clock.advance(minutes=15)
        listed = [g for g in goals.list_goals(self.settings) if g["id"] == tb["id"]][0]
        self.assertEqual(listed["remaining_seconds"], 35100)
        goals.stop_goal(None, self.settings, tb["id"])

        # Non-time-boxed goal has deadline_at=None, mode='outcome', remaining_seconds=None.
        plain = self._create(max_iterations=4)
        self.assertIsNone(plain["deadline_at"])
        self.assertEqual(plain["mode"], "outcome")
        self.assertIsNone(plain["resume_at"])
        self.assertIsNone(plain["remaining_seconds"])
        self.assertEqual(plain["rounds_passed"], 0)
        self.assertEqual(plain["rounds_total"], 0)

    def test_deadline_stops_before_new_round_and_lets_running_round_finish(self) -> None:
        goal = self._create(duration_minutes=60, mode="outcome")
        gid = goal["id"]

        # Start round 1 before deadline.
        self._advance()
        detail = self._goal(gid)
        self.assertEqual(len(detail["iterations"]), 1)
        tid = detail["iterations"][0]["task_id"]
        self.assertTrue(tid)

        # Deadline hits while round 1 is still running -> must NOT abort round 1.
        self.clock.advance(minutes=61)
        self._advance()
        still_running = self._goal(gid)
        self.assertEqual(still_running["state"], "running")
        self.assertEqual(still_running["remaining_seconds"], 0)
        self.assertEqual(len(still_running["iterations"]), 1)

        # Round 1 finishes (failed checks, evaluator suggests continue) ->
        # round 1 is recorded, but round 2 must NOT start after deadline.
        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "还差一个测试",
                "next_prompt": "修好测试",
                "question": "",
                "summary": "第 1 轮测试没通过。",
            },
            ensure_ascii=False,
        )
        self._set_task_state(tid, "failed", error="test assertion failed")
        self._advance()

        done = self._goal(gid)
        self.assertEqual(done["state"], "exhausted")
        self.assertEqual(done["stop_reason"], "deadline")
        self.assertEqual(done["summary"], "时间到了：一共跑了 1 轮，其中 0 轮通过检查。")
        self.assertEqual(len(done["iterations"]), 1)
        self.assertEqual(done["rounds_total"], 1)
        self.assertEqual(done["rounds_passed"], 0)

    def test_deadline_expired_before_round_one_stops_immediately(self) -> None:
        goal = self._create(duration_minutes=30, mode="continuous")
        self.clock.advance(minutes=31)
        self._advance()

        done = self._goal(goal["id"])
        self.assertEqual(done["state"], "exhausted")
        self.assertEqual(done["stop_reason"], "deadline")
        self.assertEqual(done["summary"], "时间到了：一共跑了 0 轮，其中 0 轮通过检查。")
        self.assertEqual(done["iterations"], [])

    def test_continuous_mode_keeps_going_after_pass_and_builds_done_prompt(self) -> None:
        goal = self._create(duration_minutes=180, mode="continuous")
        gid = goal["id"]

        # Round 1: passes checks, evaluator returns 'achieved' -> continuous mode does NOT stop!
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self.responder = lambda _p: json.dumps(
            {
                "decision": "achieved",
                "reason": "首轮目标达标",
                "next_prompt": "",
                "question": "",
                "summary": "为健康检查接口补全单元测试。",
                "round_title": "补全健康检查单元测试",
            },
            ensure_ascii=False,
        )
        self._set_task_state(t1, "completed")
        self._advance()

        after_r1 = self._goal(gid)
        self.assertEqual(after_r1["state"], "running")
        self.assertEqual(after_r1["rounds_passed"], 1)
        self.assertEqual(after_r1["rounds_total"], 2)
        self.assertTrue(after_r1["iterations"][0]["passed"])
        self.assertEqual(after_r1["iterations"][0]["round_title"], "补全健康检查单元测试")
        r2_prompt = after_r1["iterations"][1]["prompt"]
        self.assertIn("补全健康检查单元测试", r2_prompt)
        self.assertIn("已完成改进", r2_prompt)
        self.assertIn(OUTCOME, r2_prompt)

        # Round 2: passes checks with 'continue' + round_title + next_prompt -> continues to round 3.
        t2 = after_r1["iterations"][1]["task_id"]
        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "第二轮改进完成",
                "next_prompt": "继续清理冗余日志",
                "question": "",
                "summary": "优化了数据库连接池配置。",
                "round_title": "优化数据库连接池配置",
            },
            ensure_ascii=False,
        )
        self._set_task_state(t2, "completed")
        self._advance()

        after_r2 = self._goal(gid)
        self.assertEqual(after_r2["state"], "running")
        self.assertEqual(after_r2["rounds_passed"], 2)
        self.assertEqual(len(after_r2["iterations"]), 3)
        r3_prompt = after_r2["iterations"][2]["prompt"]
        self.assertIn("补全健康检查单元测试", r3_prompt)
        self.assertIn("优化数据库连接池配置", r3_prompt)
        self.assertIn("继续清理冗余日志", r3_prompt)

        # Round 3 completes after deadline -> counts all 3 passed rounds and stops with deadline.
        t3 = after_r2["iterations"][2]["task_id"]
        self.clock.advance(minutes=185)
        self._set_task_state(t3, "completed")
        self.responder = lambda _p: json.dumps(
            {
                "decision": "achieved",
                "reason": "清理完成",
                "next_prompt": "",
                "question": "",
                "summary": "清理冗余调试日志。",
                "round_title": "清理冗余调试日志",
            },
            ensure_ascii=False,
        )
        self._advance()

        final = self._goal(gid)
        self.assertEqual(final["state"], "exhausted")
        self.assertEqual(final["stop_reason"], "deadline")
        self.assertEqual(final["rounds_passed"], 3)
        self.assertEqual(final["rounds_total"], 3)
        self.assertEqual(final["summary"], "时间到了：一共跑了 3 轮，其中 3 轮通过检查。")

    def test_outcome_mode_with_deadline_still_stops_on_achieved(self) -> None:
        goal = self._create(duration_minutes=120, mode="outcome")
        gid = goal["id"]

        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._set_task_state(t1, "completed")
        self._advance()

        done = self._goal(gid)
        self.assertEqual(done["state"], "achieved")
        self.assertEqual(len(done["iterations"]), 1)

    def test_continuous_mode_three_consecutive_failures_pause_waiting_user(self) -> None:
        goal = self._create(duration_minutes=240, mode="continuous")
        gid = goal["id"]

        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "单测未通过",
                "next_prompt": "修复失败的单元测试",
                "question": "",
                "summary": "测试失败，继续尝试修复。",
            },
            ensure_ascii=False,
        )

        # Rounds 1 and 2 fail -> stay running and advance to fix.
        for expected_n in (1, 2):
            self._advance()
            cur = self._goal(gid)
            self.assertEqual(cur["state"], "running")
            tid = cur["iterations"][expected_n - 1]["task_id"]
            self._set_task_state(tid, "failed", error="pytest failed")
            self._advance()
            after = self._goal(gid)
            self.assertEqual(after["state"], "running")
            self.assertEqual(len(after["iterations"]), expected_n + 1)

        # Round 3 also fails (3 in a row) -> pause as waiting_user with Chinese question.
        t3 = self._goal(gid)["iterations"][2]["task_id"]
        self._set_task_state(t3, "failed", error="pytest failed again")
        self._advance()

        paused = self._goal(gid)
        self.assertEqual(paused["state"], "waiting_user")
        self.assertEqual(paused["question"], "最近 3 轮都没通过检查：单测未通过")
        self.assertEqual(paused["consecutive_failures"], 3)
        self.assertEqual(len(paused["iterations"]), 3)

    def test_deadline_wins_over_three_failures_and_ask_user_and_posts_end_report(self) -> None:
        origin_sid = "web:web-console:r3-deadline-wins"
        goal = self._create(
            duration_minutes=60,
            mode="continuous",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "单测未通过",
                "next_prompt": "修复失败的单元测试",
                "question": "",
                "summary": "测试失败，继续尝试修复。",
            },
            ensure_ascii=False,
        )

        # Rounds 1 and 2 fail before deadline -> streak reaches 2.
        for expected_n in (1, 2):
            self._advance()
            tid = self._goal(gid)["iterations"][expected_n - 1]["task_id"]
            self.clock.advance(minutes=20)
            self._set_task_state(tid, "failed", error="pytest failed")
            self._advance()
            self.assertEqual(self._goal(gid)["state"], "running")

        # Round 3 runs, deadline passes, and round 3 fails too (3 in a row) ->
        # deadline MUST win: finish as exhausted/deadline and post end report, NOT waiting_user!
        t3 = self._goal(gid)["iterations"][2]["task_id"]
        self.clock.advance(minutes=25)  # +65 min total -> past 60 min deadline
        self._set_task_state(t3, "failed", error="pytest failed again")
        self._advance()

        done = self._goal(gid)
        self.assertEqual(done["state"], "exhausted")
        self.assertEqual(done["stop_reason"], "deadline")
        self.assertIn("时间到了", done["summary"])
        session = get_transcript_store(self.settings).get_session(origin_sid)
        msgs = [m["text"] for m in build_history(session, settings=self.settings) if m.get("role") == "assistant"]
        self.assertTrue(any("的目标结束了：时间到了。" in txt for txt in msgs))

    def test_deadline_wins_over_ask_user_give_up_unavailable_and_needs_validation(self) -> None:
        cases = [
            ("ask_user", json.dumps({"decision": "ask_user", "question": "需要确认？", "summary": "等确认"}, ensure_ascii=False), "failed"),
            ("give_up", json.dumps({"decision": "give_up", "reason": "做不下去", "summary": "放弃"}, ensure_ascii=False), "failed"),
            ("unparsable", "not-json", "failed"),
            ("waiting_user", json.dumps({"decision": "ask_user", "question": "没配验证", "summary": "等验证"}, ensure_ascii=False), "waiting_user"),
        ]
        for label, reply, task_state in cases:
            with self.subTest(case=label):
                origin_sid = f"web:web-console:r3-dead-{label}"
                goal = self._create(duration_minutes=60, mode="continuous", origin_session_id=origin_sid)
                gid = goal["id"]
                self._advance()
                t1 = self._goal(gid)["iterations"][0]["task_id"]
                self.clock.advance(minutes=65)
                if task_state == "waiting_user":
                    ags.transition(self.settings, t1, "waiting_user", stage="needs_validation", error="no validation")
                else:
                    self._set_task_state(t1, task_state, error="check failed")
                self.responder = lambda _p, r=reply: r
                self._advance()

                done = self._goal(gid)
                self.assertEqual(done["state"], "exhausted")
                self.assertEqual(done["stop_reason"], "deadline")
                self.assertIn("时间到了", done["summary"])
                session = get_transcript_store(self.settings).get_session(origin_sid)
                msgs = [m["text"] for m in build_history(session, settings=self.settings) if m.get("role") == "assistant"]
                self.assertTrue(any("的目标结束了：时间到了。" in txt for txt in msgs))

    def test_outcome_mode_achieved_after_deadline_still_finishes_as_achieved(self) -> None:
        goal = self._create(duration_minutes=60, mode="outcome")
        gid = goal["id"]
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self.clock.advance(minutes=65)
        self._set_task_state(t1, "completed")
        self._advance()

        done = self._goal(gid)
        self.assertEqual(done["state"], "achieved")

    def test_is_infrastructure_failure_classifies_from_structured_fields(self) -> None:
        # 1) Pi length stop (even when error message is Chinese, classified from metadata stop_reason)
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t1", "state": "failed", "stage": "implementation", "error": "随便什么中文错误"},
                executions=[{
                    "executor": "pi",
                    "status": "failed",
                    "error": "随便什么中文错误",
                    "metadata_json": json.dumps({"stop_reason": "length"}),
                }],
            )
        )
        # 2) Executor timeout (status == 'timeout')
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t2", "state": "failed", "stage": "implementation", "error": ""},
                executions=[{"executor": "claude", "status": "timeout", "error": "", "metadata_json": "{}"}],
            )
        )
        # 3) Quota / rate limit via error_kind
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t3", "state": "failed", "stage": "implementation", "metadata": {"error_kind": "quota"}},
                executions=[],
            )
        )
        # 4) Executor unavailable via error_kind
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t4", "state": "failed", "stage": "supervisor_error", "metadata": {"error_kind": "executor_unavailable"}},
                executions=[],
            )
        )
        # 5) supervisor_error with runtime cause (lock_error / analysis failed) vs worktree mutation
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t5", "state": "failed", "stage": "supervisor_error", "error": "worktree locked", "metadata": {"error_kind": "lock_error"}},
                executions=[],
            )
        )
        self.assertFalse(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t6", "state": "failed", "stage": "supervisor_error", "error": "worktree changed during analysis (rejected before implementation)", "metadata": {"error_kind": "worktree_mutated"}},
                executions=[],
            )
        )
        # 6) Code / validation failure is NOT infra even if Chinese text mentions timeout
        self.assertFalse(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t7", "state": "failed", "stage": "failed", "error": "Pi 响应未完整结束（length）"},
                executions=[],
            )
        )
        self.assertFalse(
            goals._is_infrastructure_failure(
                self.settings,
                {"id": "t8", "state": "failed", "stage": "repair_limit_exceeded", "error": "max repair rounds exceeded; remaining issues reported"},
                executions=[{"executor": "pi", "status": "finished", "error": "", "metadata_json": "{}"}],
            )
        )
        # 7) Three invalid review format attempts (review_format) count as runtime/infra failure
        self.assertTrue(
            goals._is_infrastructure_failure(
                self.settings,
                {
                    "id": "t9",
                    "state": "failed",
                    "stage": "review",
                    "error": "审阅结果的格式三次都不对，这一轮没能完成审阅。",
                    "metadata": {"error_kind": "review_format"},
                },
                executions=[],
            )
        )

    def test_infrastructure_failures_not_counted_toward_continuous_streak(self) -> None:
        goal = self._create(duration_minutes=240, mode="continuous")
        gid = goal["id"]

        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "上一轮中断，继续重试",
                "next_prompt": "继续完成目标",
                "question": "",
                "summary": "执行器遇到问题，重试。",
            },
            ensure_ascii=False,
        )

        # 2 code failures -> streak = 2
        for idx in (1, 2):
            self._advance()
            tid = self._goal(gid)["iterations"][idx - 1]["task_id"]
            self._set_task_state(tid, "failed", error="pytest failed")
            self._advance()
            self.assertEqual(self._goal(gid)["consecutive_failures"], idx)

        # Round 3: Pi length stop (via execution metadata) -> NOT counted, streak stays 2, keeps running!
        self._advance()
        t3 = self._goal(gid)["iterations"][2]["task_id"]
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, status, started_at, error, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("ex-pi-len", t3, "implementer", "pi", "failed",
                     "2026-11-15T10:00:00+00:00", "模型两次都因为回答太长被截断",
                     json.dumps({"stop_reason": "length", "error_kind": "pi_length"})),
                )
        finally:
            conn.close()
        ags.transition(self.settings, t3, "failed", stage="implementation",
                       error="implementation failed: 模型两次都因为回答太长被截断",
                       metadata={"error_kind": "pi_length"})
        self._advance()
        after_r3 = self._goal(gid)
        self.assertEqual(after_r3["state"], "running")
        self.assertEqual(after_r3["consecutive_failures"], 2)

        # Round 4: executor timeout -> NOT counted, streak stays 2, keeps running!
        t4 = after_r3["iterations"][3]["task_id"]
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, status, started_at, error, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    ("ex-timeout", t4, "implementer", "pi", "timeout",
                     "2026-11-15T10:05:00+00:00", "timed out after 3600s",
                     json.dumps({"error_kind": "executor_timeout"})),
                )
        finally:
            conn.close()
        ags.transition(self.settings, t4, "failed", stage="implementation",
                       error="implementation failed: timed out after 3600s",
                       metadata={"error_kind": "executor_timeout"})
        self._advance()
        after_r4 = self._goal(gid)
        self.assertEqual(after_r4["state"], "running")
        self.assertEqual(after_r4["consecutive_failures"], 2)

        # Round 5: 3rd real code failure -> streak hits 3 -> waiting_user with last reason!
        t5 = after_r4["iterations"][4]["task_id"]
        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "缺少 scripts/check.sh",
                "next_prompt": "补上 check.sh",
                "question": "",
                "summary": "验证脚本缺失。",
            },
            ensure_ascii=False,
        )
        self._set_task_state(t5, "failed", error="check-script failed")
        self._advance()
        paused = self._goal(gid)
        self.assertEqual(paused["state"], "waiting_user")
        self.assertEqual(paused["consecutive_failures"], 3)
        self.assertEqual(paused["question"], "最近 3 轮都没通过检查：缺少 scripts/check.sh")

    def test_quota_pause_does_not_burn_round_and_resumes_at_reset(self) -> None:
        goal = self._create(duration_minutes=180, mode="continuous")
        gid = goal["id"]

        # Round 1 succeeds -> round 2 dispatches.
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._set_task_state(t1, "completed")
        self._advance()

        before_quota = self._goal(gid)
        self.assertEqual(len(before_quota["iterations"]), 2)
        t2 = before_quota["iterations"][1]["task_id"]
        r2_prompt = before_quota["iterations"][1]["prompt"]

        # Seed cooling reset time in workers state (+15 minutes).
        reset_ts = int((self.clock.now() + timedelta(minutes=15)).timestamp())
        state_file = self.root / "memory" / "state" / "workers.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"cooling": {"codex": {"reset_at": reset_ts, "reason": "429"}}}),
            encoding="utf-8",
        )

        # Round 2 fails because all workers hit quota.
        eval_calls_before = self.fake.calls
        self._set_task_state(t2, "failed", error=f"{worker_pool.ALL_QUOTA_PREFIX}429 rate limit exceeded")
        self._advance()

        paused = self._goal(gid)
        self.assertEqual(paused["state"], "waiting_quota")
        self.assertEqual(paused["resume_at"], "2026-11-15T10:15:00+00:00")
        self.assertEqual(paused["rounds_total"], 1)  # Round 2 did NOT burn a round
        self.assertEqual(len(paused["iterations"]), 2)
        self.assertEqual(paused["iterations"][1]["task_id"], "")
        self.assertEqual(paused["iterations"][1]["prompt"], r2_prompt)
        self.assertEqual(self.fake.calls, eval_calls_before)  # evaluator not invoked on quota pause
        self.assertIn("自动继续", paused["summary"])
        self.assertIn("大约 15 分钟后自动继续", paused["summary"])
        self.assertNotRegex(paused["summary"], r"\b\d{1,2}:\d{2}\b")
        # No extra timer thread: the pause is driven by the existing goal-loop tick.
        self.assertFalse(
            [t for t in threading.enumerate() if isinstance(t, threading.Timer)],
            "quota pause must not start its own timer thread",
        )

        # Before resume_at: stays waiting_quota.
        self.clock.advance(minutes=10)
        self._advance()
        self.assertEqual(self._goal(gid)["state"], "waiting_quota")

        # At resume_at: automatically resumes and retries round n=2 cleanly.
        self.clock.advance(minutes=5)
        self._advance()
        resumed = self._goal(gid)
        self.assertEqual(resumed["state"], "running")
        self.assertIsNone(resumed["resume_at"])
        self.assertEqual(len(resumed["iterations"]), 2)
        self.assertTrue(resumed["iterations"][1]["task_id"])
        self.assertNotEqual(resumed["iterations"][1]["task_id"], t2)
        self.assertEqual(resumed["iterations"][1]["prompt"], r2_prompt)

    def test_quota_pause_falls_back_to_30_minutes_when_reset_unknown(self) -> None:
        goal = self._create(duration_minutes=120, mode="outcome")
        self._advance()
        t1 = self._goal(goal["id"])["iterations"][0]["task_id"]
        self._set_task_state(t1, "failed", error=f"{worker_pool.ALL_QUOTA_PREFIX}usage limit reached")
        self._advance()

        paused = self._goal(goal["id"])
        self.assertEqual(paused["state"], "waiting_quota")
        self.assertEqual(paused["resume_at"], "2026-11-15T10:30:00+00:00")

    def test_quota_reset_after_deadline_stops_with_quota_reason(self) -> None:
        goal = self._create(duration_minutes=60, mode="continuous")
        self._advance()
        t1 = self._goal(goal["id"])["iterations"][0]["task_id"]

        # Earliest reset is +75 min, which exceeds the 60-min deadline.
        reset_ts = int((self.clock.now() + timedelta(minutes=75)).timestamp())
        state_file = self.root / "memory" / "state" / "workers.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"cooling": {"codex": {"reset_at": reset_ts, "reason": "quota"}}}),
            encoding="utf-8",
        )
        self._set_task_state(t1, "failed", error=f"{worker_pool.ALL_QUOTA_PREFIX}429")
        self._advance()

        stopped = self._goal(goal["id"])
        self.assertEqual(stopped["state"], "exhausted")
        self.assertEqual(stopped["stop_reason"], "quota")
        self.assertEqual(stopped["summary"], goals.QUOTA_EXCEEDED_DEADLINE_SUMMARY)
        self.assertIsNone(stopped["resume_at"])

    def test_restart_rearms_waiting_quota_and_resumes_due_goal(self) -> None:
        future_goal = self._create(duration_minutes=180, mode="continuous")
        goals._update_goal(
            self.settings,
            future_goal["id"],
            state=goals.GOAL_WAITING_QUOTA,
            resume_at="2026-11-15T10:20:00+00:00",
            updated_at=goals._utc_now(),
        )
        due_goal = self._create(duration_minutes=180, mode="continuous")
        goals._update_goal(
            self.settings,
            due_goal["id"],
            state=goals.GOAL_WAITING_QUOTA,
            resume_at="2026-11-15T09:59:00+00:00",
            updated_at=goals._utc_now(),
        )

        # Simulate process restart: a fresh startup pass, then the normal tick.
        pending = goals.recover_on_startup(None, self.settings)

        self.assertEqual(pending, [future_goal["id"]])
        self.assertEqual(self._goal(future_goal["id"])["state"], "waiting_quota")
        self.assertEqual(self._goal(due_goal["id"])["state"], "running")
        self.assertIsNone(self._goal(due_goal["id"])["resume_at"])

        # The remaining pause resumes on the goal loop's own tick once resume_at arrives.
        self.clock.advance(minutes=19)
        self._advance()
        self.assertEqual(self._goal(future_goal["id"])["state"], "waiting_quota")
        self.clock.advance(minutes=1)
        self._advance()
        resumed = self._goal(future_goal["id"])
        self.assertEqual(resumed["state"], "running")
        self.assertIsNone(resumed["resume_at"])

    def test_waiting_quota_past_deadline_finishes_with_deadline(self) -> None:
        goal = self._create(duration_minutes=30, mode="continuous")
        goals._update_goal(
            self.settings,
            goal["id"],
            state=goals.GOAL_WAITING_QUOTA,
            resume_at="2026-11-15T10:20:00+00:00",
            updated_at=goals._utc_now(),
        )
        self.clock.advance(minutes=31)
        goals.recover_on_startup(None, self.settings)

        done = self._goal(goal["id"])
        self.assertEqual(done["state"], "exhausted")
        self.assertEqual(done["stop_reason"], "deadline")
        self.assertIsNone(done["resume_at"])

    def test_stop_while_waiting_quota_is_final(self) -> None:
        goal = self._create(duration_minutes=120, mode="continuous")
        goals._update_goal(
            self.settings,
            goal["id"],
            state=goals.GOAL_WAITING_QUOTA,
            resume_at="2026-11-15T10:10:00+00:00",
            updated_at=goals._utc_now(),
        )
        stopped = goals.stop_goal(None, self.settings, goal["id"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertIsNone(stopped["resume_at"])

        self.clock.advance(minutes=15)
        self._advance()
        self.assertEqual(self._goal(goal["id"])["state"], "stopped")

    def test_check_output_merely_containing_429_is_not_a_quota_pause(self) -> None:
        """A failed repo check must never pause the goal as "out of quota".

        Only structured pool signals (the pool's own markers, cooling /
        over-threshold state) and a provider's own quota message may do that.
        """
        goal = self._create(duration_minutes=120, mode="outcome")
        gid = goal["id"]
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._seed_failed_check_round(t1, CHECK_OUTPUT_WITH_429)
        self._set_task_state(t1, "failed",
                             error="max repair rounds exceeded; remaining issues reported")
        self.responder = lambda _p: json.dumps(
            {
                "decision": "continue",
                "reason": "单元测试还是没过",
                "next_prompt": "把 divide 除数为 0 的处理补上",
                "question": "",
                "summary": "第 1 轮测试没通过。",
            },
            ensure_ascii=False,
        )
        self._advance()

        after = self._goal(gid)
        self.assertEqual(after["state"], "running")
        self.assertIsNone(after["resume_at"])
        self.assertNotIn("额度", after["summary"])
        # The round was evaluated (round 2 dispatched), not paused and retried.
        self.assertEqual(after["iterations"][0]["task_id"], t1)
        self.assertEqual(len(after["iterations"]), 2)

    def test_provider_quota_error_from_an_executor_still_pauses(self) -> None:
        goal = self._create(duration_minutes=120, mode="outcome")
        gid = goal["id"]
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, status, started_at, error)
                       VALUES (?,?,?,?,?,?,?)""",
                    ("ex-impl01", t1, "implementer", "pool", "failed",
                     "2026-11-15T10:00:00+00:00", PROVIDER_QUOTA_ERROR),
                )
        finally:
            conn.close()
        self._set_task_state(t1, "failed", error=f"implementation failed: {PROVIDER_QUOTA_ERROR}")
        self._advance()

        paused = self._goal(gid)
        self.assertEqual(paused["state"], "waiting_quota")
        self.assertEqual(paused["resume_at"], "2026-11-15T10:30:00+00:00")

    def test_quota_failure_after_stop_keeps_round_task(self) -> None:
        goal = self._create(duration_minutes=120, mode="continuous")
        gid = goal["id"]
        self._advance()
        t1 = self._goal(gid)["iterations"][0]["task_id"]
        self._set_task_state(t1, "failed", error=f"{worker_pool.ALL_QUOTA_PREFIX}429")
        # Owner stops the goal before the loop looks at the failed round.
        goals._update_goal(self.settings, gid, state=goals.GOAL_STOPPED, updated_at=goals._utc_now())
        iteration = goals._iterations(self.settings, gid)[0]
        task = ags.get_task(self.settings, t1) or {}
        handled = goals._maybe_handle_quota_failure(
            None, self.settings, goals._get_goal_row(self.settings, gid), iteration, [iteration], task
        )
        self.assertFalse(handled)
        after = self._goal(gid)
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(after["iterations"][0]["task_id"], t1)

    def test_old_database_migrates_missing_columns(self) -> None:
        fresh_root = self.root / "legacy"
        legacy_settings = _make_settings(fresh_root)
        repo, _ = svc.register_repository(legacy_settings, path=str(self.repo_dir))
        wt = svc.create_managed_worktree(legacy_settings, repo["id"], branch="feat/legacy-db")

        db_path = goals._db_path(legacy_settings)
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE supervisor_goals (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                worktree_id TEXT NOT NULL,
                title TEXT NOT NULL,
                outcome TEXT NOT NULL,
                max_iterations INTEGER NOT NULL DEFAULT 5,
                role_overrides_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                stop_reason TEXT NOT NULL DEFAULT '',
                last_next_prompt TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE supervisor_goal_iterations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id TEXT NOT NULL,
                n INTEGER NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL DEFAULT '',
                decision_reason TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                task_final_state TEXT NOT NULL DEFAULT '',
                evaluating INTEGER NOT NULL DEFAULT 0,
                evaluating_since TEXT NOT NULL DEFAULT '',
                UNIQUE(goal_id, n)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO supervisor_goals
                (id, repo_id, worktree_id, title, outcome, max_iterations, state, created_at, updated_at)
            VALUES
                ('goal-legacy01', ?, ?, '旧目标', '完成老任务', 5, 'achieved',
                 '2026-11-14T10:00:00+00:00', '2026-11-14T10:05:00+00:00')
            """,
            (str(repo["id"]), str(wt["id"])),
        )
        conn.execute(
            """
            INSERT INTO supervisor_goal_iterations
                (goal_id, n, task_id, prompt, decision, decision_reason, task_final_state)
            VALUES
                ('goal-legacy01', 1, 'task-old', '完成老任务', 'achieved', '已达成', 'completed')
            """
        )
        conn.commit()
        conn.close()

        listed = goals.list_goals(legacy_settings)
        self.assertEqual(len(listed), 1)
        legacy = listed[0]
        self.assertEqual(legacy["id"], "goal-legacy01")
        self.assertIsNone(legacy["deadline_at"])
        self.assertEqual(legacy["mode"], "outcome")
        self.assertIsNone(legacy["resume_at"])
        self.assertIsNone(legacy["remaining_seconds"])
        self.assertEqual(legacy["rounds_total"], 1)
        self.assertEqual(legacy["rounds_passed"], 1)
        self.assertIsNone(legacy["last_digest_at"])
        self.assertIsNone(legacy["last_digest_rounds"])


class ProgressDigestTests(_TimeboxedGoalBase):
    """Hermetic tests for Task G2: short progress digests during long goals."""

    def _make_test_settings(self) -> Any:
        return dataclasses.replace(
            _make_settings(self.root),
            telegram_bot_token="tg-token",
            telegram_allowed_user_id=42,
            lark_app_id="lark-app",
            lark_app_secret="lark-secret",
            lark_allowed_open_id="ou-owner",
        )

    def setUp(self) -> None:
        super().setUp()

        self.telegram_sent: list[str] = []
        self.feishu_sent: list[str] = []
        self.web_pushed: list[tuple[str, str]] = []

        tg_patch = patch(
            "personal_tools.sentry.send_owner_telegram",
            side_effect=lambda _s, text, **_kw: self.telegram_sent.append(text) or True,
        )
        fs_patch = patch(
            "personal_tools.sentry.send_owner_feishu",
            side_effect=lambda _s, text, **_kw: self.feishu_sent.append(text) or True,
        )
        push_patch = patch.object(
            web_push,
            "queue_notification",
            side_effect=lambda _s, item_id, kind: self.web_pushed.append((item_id, kind)),
        )
        for p in (tg_patch, fs_patch, push_patch):
            p.start()
            self.addCleanup(p.stop)

    def _session_messages(self, session_id: str) -> list[dict[str, Any]]:
        session = get_transcript_store(self.settings).get_session(session_id)
        return [
            m for m in build_history(session, settings=self.settings)
            if m.get("role") == "assistant"
        ]

    def _digest_messages(self, session_id: str) -> list[str]:
        return [
            m["text"] for m in self._session_messages(session_id)
            if "还在跑：" in m.get("text", "")
        ]

    def _run_round(
        self,
        goal_id: str,
        *,
        minutes: int = 15,
        passed: bool = True,
        round_title: str = "",
        decision: str = "achieved",
        next_prompt: str = "继续优化下一处",
    ) -> str:
        self._advance()
        detail = self._goal(goal_id)
        task_id = detail["iterations"][-1]["task_id"]
        self.assertTrue(task_id)
        self.clock.advance(minutes=minutes)
        if passed:
            self.responder = lambda _p, d=decision, rt=round_title, np=next_prompt: json.dumps(
                {
                    "decision": d,
                    "reason": "本轮检查通过",
                    "next_prompt": np,
                    "question": "",
                    "summary": rt or "本轮改进完成。",
                    "round_title": rt,
                },
                ensure_ascii=False,
            )
            self._set_task_state(task_id, "completed")
        else:
            self.responder = lambda _p, np=next_prompt: json.dumps(
                {
                    "decision": "continue",
                    "reason": "测试未通过",
                    "next_prompt": np,
                    "question": "",
                    "summary": "本轮检查没通过，继续修。",
                    "round_title": "",
                },
                ensure_ascii=False,
            )
            self._set_task_state(task_id, "failed", error="check failed")
        self._advance()
        goal_notify.drain()
        return task_id

    def test_digest_after_3_rounds_posts_to_origin_and_pushes_channels(self) -> None:
        origin_sid = "web:web-console:g2-origin-session"
        goal = self._create(
            duration_minutes=600,
            mode="continuous",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        # Rounds 1 and 2 (< 3 rounds, < 60 min): no digest yet.
        self._run_round(gid, minutes=20, round_title="补全单测")
        self._run_round(gid, minutes=20, round_title="修复分页边界")
        self.assertEqual(self._digest_messages(origin_sid), [])
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self.feishu_sent, [])
        self.assertIsNone(self._goal(gid)["last_digest_at"])
        self.assertIsNone(self._goal(gid)["last_digest_rounds"])

        # Round 3 (+55m total): triggers digest #1.
        self._run_round(gid, minutes=15, round_title="清理调试日志")
        digests = self._digest_messages(origin_sid)
        self.assertEqual(len(digests), 1)
        expected_1 = (
            "repo 还在跑：已经 55 分，跑了 3 轮、通过 3 轮，还剩约 9 小时 5 分。"
            "最近完成：补全单测、修复分页边界、清理调试日志。"
        )
        self.assertEqual(digests[0], expected_1)
        self.assertEqual(self.telegram_sent, [expected_1])
        self.assertEqual(self.feishu_sent, [expected_1])
        self.assertEqual(self.web_pushed, [])

        after_d1 = self._goal(gid)
        self.assertEqual(after_d1["last_digest_rounds"], 3)
        self.assertEqual(after_d1["last_digest_at"], "2026-11-15T10:55:00+00:00")

        # Rounds 4 and 5 (+70m, +85m): only 1-2 rounds and < 60m since digest #1 -> quiet.
        self._run_round(gid, minutes=15, round_title="优化配置读取")
        self._run_round(gid, minutes=15, round_title="收紧超时处理")
        self.assertEqual(len(self._digest_messages(origin_sid)), 1)

        # Round 6 (+100m total): 3 rounds since digest #1 -> triggers digest #2 (at most 3 round_titles).
        self._run_round(gid, minutes=15, round_title="增加错误提示")
        digests_2 = self._digest_messages(origin_sid)
        self.assertEqual(len(digests_2), 2)
        self.assertIn("跑了 6 轮、通过 6 轮", digests_2[1])
        self.assertIn("最近完成：优化配置读取、收紧超时处理、增加错误提示。", digests_2[1])
        self.assertNotIn("补全单测", digests_2[1])
        self.assertEqual(len(self.telegram_sent), 2)
        self.assertEqual(len(self.feishu_sent), 2)
        self.assertEqual(self._goal(gid)["last_digest_rounds"], 6)

    def test_digest_after_60_minutes_with_fewer_rounds_and_default_session_fallback(self) -> None:
        default_sid = agents.session_id_for(agents.DEFAULT_AGENT_ID)
        goal = self._create(duration_minutes=600, mode="continuous")
        gid = goal["id"]

        # Round 1 takes 65 minutes (only 1 round, >= 60 min): digest #1 fires to default main chat.
        self._run_round(gid, minutes=65, round_title="完成首轮排查")
        digests = self._digest_messages(default_sid)
        self.assertEqual(len(digests), 1)
        self.assertEqual(
            digests[0],
            "repo 还在跑：已经 1 小时 5 分，跑了 1 轮、通过 1 轮，还剩约 8 小时 55 分。"
            "最近完成：完成首轮排查。",
        )
        self.assertEqual(self.telegram_sent, digests)
        self.assertEqual(self.feishu_sent, digests)

        # Round 2 takes 20 min (only 20 min since digest #1, 1 round): no digest.
        self._run_round(gid, minutes=20, round_title="补强输入校验")
        self.assertEqual(len(self._digest_messages(default_sid)), 1)

        # Round 3 takes 45 min (65 min since digest #1, 2 rounds since): fires by 60-min rule!
        self._run_round(gid, minutes=45, round_title="整理文档")
        digests_2 = self._digest_messages(default_sid)
        self.assertEqual(len(digests_2), 2)
        self.assertIn("已经 2 小时 10 分，跑了 3 轮、通过 3 轮，还剩约 7 小时 50 分。", digests_2[1])
        self.assertEqual(self._goal(gid)["last_digest_rounds"], 3)

    def test_no_duplicate_after_restart(self) -> None:
        origin_sid = "web:web-console:g2-restart"
        goal = self._create(
            duration_minutes=600,
            mode="continuous",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        for title in ("第一项", "第二项", "第三项"):
            self._run_round(gid, minutes=15, round_title=title)
        self.assertEqual(len(self._digest_messages(origin_sid)), 1)
        self.assertEqual(len(self.telegram_sent), 1)
        self.assertEqual(len(self.feishu_sent), 1)

        # Simulate process restart (DB connection cache cleared, notify worker reloaded, 70 min passed).
        goals._INITIALIZED_DBS.clear()
        importlib.reload(goal_notify)
        self.clock.advance(minutes=70)

        # Re-evaluating / re-calling digest check on the already-digested round must not duplicate.
        row = goals._get_goal_row(self.settings, gid)
        iters = goals._iterations(self.settings, gid)
        completed = [it for it in iters if goals._iteration_completed(it)]
        self.assertFalse(goals._maybe_post_progress_digest(self.settings, row, completed))
        goal_notify.notify_goal_digest(
            self.settings,
            row,
            "duplicate-check",
            rounds=3,
            digest_at=str(row["last_digest_at"] or ""),
        )
        goal_notify.drain()

        self.assertEqual(len(self._digest_messages(origin_sid)), 1)
        self.assertEqual(len(self.telegram_sent), 1)
        self.assertEqual(len(self.feishu_sent), 1)

        # Completing round 4 (> 60 min since digest #1) does emit digest #2 once.
        t4 = self._goal(gid)["iterations"][-1]["task_id"]
        self.clock.advance(minutes=5)
        self._set_task_state(t4, "completed")
        self.responder = lambda _p: json.dumps(
            {
                "decision": "achieved",
                "reason": "第四轮通过",
                "next_prompt": "",
                "question": "",
                "summary": "第四项完成。",
                "round_title": "第四项",
            },
            ensure_ascii=False,
        )
        self._advance()
        goal_notify.drain()
        self.assertEqual(len(self._digest_messages(origin_sid)), 2)
        self.assertEqual(len(self.telegram_sent), 2)
        self.assertEqual(len(self.feishu_sent), 2)

    def test_no_digest_for_round_capped_goal(self) -> None:
        origin_sid = "web:web-console:g2-round-capped"
        goal = self._create(
            max_iterations=6,
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        for idx in range(1, 5):
            self._run_round(
                gid,
                minutes=30,
                decision="continue",
                round_title=f"第{idx}项",
                next_prompt="继续下一轮",
            )

        self.assertEqual(self._digest_messages(origin_sid), [])
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self.feishu_sent, [])
        self.assertIsNone(self._goal(gid)["last_digest_at"])
        self.assertIsNone(self._goal(gid)["last_digest_rounds"])

    def test_no_digest_after_goal_ends(self) -> None:
        # 1) Outcome time-boxed goal achieved on round 3 -> finish report only, no digest.
        sid_a = "web:web-console:g2-end-achieved"
        g_ach = self._create(duration_minutes=300, mode="outcome", origin_session_id=sid_a)
        for idx in (1, 2):
            self._run_round(
                g_ach["id"],
                minutes=15,
                decision="continue",
                round_title=f"完成第{idx}步",
                next_prompt="继续下一步",
            )
        self._run_round(g_ach["id"], minutes=15, decision="achieved", round_title="最终收尾")
        self.assertEqual(self._goal(g_ach["id"])["state"], "achieved")
        self.assertEqual(self._digest_messages(sid_a), [])
        self.assertTrue(any("的目标结束了：已达成。" in m["text"] for m in self._session_messages(sid_a)))

        # 2) Continuous goal whose round 3 finishes after deadline -> finish report only, no digest.
        sid_b = "web:web-console:g2-end-deadline"
        g_dead = self._create(duration_minutes=60, mode="continuous", origin_session_id=sid_b)
        self._run_round(g_dead["id"], minutes=20, round_title="步1")
        self._run_round(g_dead["id"], minutes=20, round_title="步2")
        self._run_round(g_dead["id"], minutes=25, round_title="步3")
        self.assertEqual(self._goal(g_dead["id"])["state"], "exhausted")
        self.assertEqual(self._digest_messages(sid_b), [])
        self.assertTrue(any("的目标结束了：时间到了。" in m["text"] for m in self._session_messages(sid_b)))
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self.feishu_sent, [])

        # 3) Stopped goal never gets a digest even if helper is invoked late.
        sid_c = "web:web-console:g2-end-stopped"
        g_stop = self._create(duration_minutes=300, mode="continuous", origin_session_id=sid_c)
        self._run_round(g_stop["id"], minutes=70, round_title="停前完成")
        self.assertEqual(len(self._digest_messages(sid_c)), 1)
        goals.stop_goal(None, self.settings, g_stop["id"])
        self.clock.advance(minutes=70)
        self.assertFalse(
            goals._maybe_post_progress_digest(
                self.settings,
                goals._get_goal_row(self.settings, g_stop["id"]),
                goals._iterations(self.settings, g_stop["id"]),
            )
        )
        self.assertEqual(len(self._digest_messages(sid_c)), 1)

    def test_failing_streak_wording(self) -> None:
        origin_sid = "web:web-console:g2-failing-streak"
        goal = self._create(
            duration_minutes=600,
            mode="outcome",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        for _ in range(3):
            self._run_round(gid, minutes=20, passed=False, next_prompt="继续修复失败测试")

        self.assertEqual(self._goal(gid)["state"], "running")
        digests = self._digest_messages(origin_sid)
        self.assertEqual(len(digests), 1)
        self.assertEqual(
            digests[0],
            "repo 还在跑：已经 1 小时，跑了 3 轮、通过 0 轮，还剩约 9 小时。"
            "最近 3 轮都没通过检查，正在继续修。",
        )
        self.assertNotIn("最近完成：", digests[0])
        self.assertEqual(self.telegram_sent, digests)
        self.assertEqual(self.feishu_sent, digests)

    def test_usage_line_present_only_with_usage(self) -> None:
        origin_sid = "web:web-console:g2-usage"
        goal = self._create(
            duration_minutes=600,
            mode="continuous",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        # First 3 rounds: no usage recorded -> no usage line.
        for idx in range(1, 4):
            self._run_round(gid, minutes=15, round_title=f"无用量轮{idx}")
        digests_no_usage = self._digest_messages(origin_sid)
        self.assertEqual(len(digests_no_usage), 1)
        self.assertNotIn("已用", digests_no_usage[0])
        self.assertNotIn("token", digests_no_usage[0])

        # Seed usage for round 4 and advance >= 60 minutes -> usage line appears.
        t4 = self._goal(gid)["iterations"][-1]["task_id"]
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, status, started_at, finished_at)
                       VALUES (?, ?, 'implementer', 'fake', 'finished', ?, ?)""",
                    ("ex-g2-u1", t4, "2026-11-15T10:50:00+00:00", "2026-11-15T10:55:00+00:00"),
                )
                ags._store_execution_usage(
                    conn,
                    execution_id="ex-g2-u1",
                    task_id=t4,
                    role="implementer",
                    executor="fake",
                    model="test-model",
                    usage={"input_tokens": 30000, "output_tokens": 4000, "cached_input_tokens": 0},
                )
        finally:
            conn.close()

        self.clock.advance(minutes=65)
        self._set_task_state(t4, "completed")
        self.responder = lambda _p: json.dumps(
            {
                "decision": "achieved",
                "reason": "有用量轮通过",
                "next_prompt": "",
                "question": "",
                "summary": "有用量轮完成。",
                "round_title": "有用量轮",
            },
            ensure_ascii=False,
        )
        self._advance()
        goal_notify.drain()

        digests_with_usage = self._digest_messages(origin_sid)
        self.assertEqual(len(digests_with_usage), 2)
        lines = digests_with_usage[1].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "已用约 3.4 万 token。")
        self.assertEqual(self.telegram_sent[-1], digests_with_usage[1])
        self.assertEqual(self.feishu_sent[-1], digests_with_usage[1])

    def test_flag_off_posts_nothing_and_env_flag_defaults_on(self) -> None:
        env = {
            "CODEX_WORKSPACE_ROOT": str(self.root / "ws"),
            "CODEX_TASK_ROOT": str(self.root / "tasks"),
            "CODEX_MEMORY_ROOT": str(self.root / "mem-env"),
            "TELEGRAM_ALLOWED_USER_ID": "1",
        }
        (self.root / "ws").mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(load_runtime_settings(env_file="").goal_digests_enabled)
        with patch.dict(os.environ, {**env, "CONVEYOR_GOAL_DIGESTS": "false"}, clear=True):
            self.assertFalse(load_runtime_settings(env_file="").goal_digests_enabled)

        self.settings = dataclasses.replace(self.settings, goal_digests_enabled=False)
        origin_sid = "web:web-console:g2-flag-off"
        goal = self._create(
            duration_minutes=600,
            mode="continuous",
            origin_session_id=origin_sid,
        )
        gid = goal["id"]

        for idx in range(1, 5):
            self._run_round(gid, minutes=30, round_title=f"静默轮{idx}")

        self.assertEqual(self._digest_messages(origin_sid), [])
        self.assertEqual(self.telegram_sent, [])
        self.assertEqual(self.feishu_sent, [])
        self.assertEqual(self.web_pushed, [])
        self.assertIsNone(self._goal(gid)["last_digest_at"])
        self.assertIsNone(self._goal(gid)["last_digest_rounds"])


if __name__ == "__main__":
    unittest.main()


class LateRoundStartTests(_TimeboxedGoalBase):
    """R11: a time-boxed goal does not start a round it cannot finish in time."""

    def test_new_30_minute_goal_still_starts_round_one(self) -> None:
        goal = self._create(duration_minutes=30)
        self._advance()
        fresh = self._goal(goal["id"])
        self.assertEqual(fresh["state"], "running")
        self.assertEqual(len(fresh["iterations"]), 1)

    def test_first_round_with_four_minutes_left_finishes_with_reason(self) -> None:
        goal = self._create(duration_minutes=30)
        self.clock.advance(minutes=26)
        self._advance()
        done = self._goal(goal["id"])
        self.assertEqual(done["state"], goals.GOAL_EXHAUSTED)
        self.assertEqual(done["stop_reason"], goals.STOP_REASON_DEADLINE)
        self.assertIn("只剩约 4 分钟，不够再跑一轮", done["summary"])
        self.assertEqual(len(done["iterations"]), 0)

    def test_env_zero_disables_the_check(self) -> None:
        with patch.dict(os.environ, {"CONVEYOR_GOAL_MIN_ROUND_START_SECONDS": "0"}):
            goal = self._create(duration_minutes=30)
            self.clock.advance(minutes=29)
            self._advance()
            self.assertEqual(len(self._goal(goal["id"])["iterations"]), 1)

    def test_malformed_env_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {"CONVEYOR_GOAL_MIN_ROUND_START_SECONDS": "abc"}):
            self.assertEqual(goals._min_round_start_seconds(), 300)

    def _round(self, start_min_ago: float, length_min: float) -> dict[str, Any]:
        start = self.clock.now() - timedelta(minutes=start_min_ago)
        return {
            "started_at": goals._format_utc(start),
            "finished_at": goals._format_utc(start + timedelta(minutes=length_min)),
            "passed": True,
        }

    def _deadline_goal(self, minutes_left: float) -> dict[str, Any]:
        return {"id": "g", "deadline_at": goals._format_utc(self.clock.now() + timedelta(minutes=minutes_left))}

    def test_long_rounds_and_nine_minutes_left_stop(self) -> None:
        iterations = [self._round(60, 20), self._round(40, 24)]
        with patch.object(goals, "_enrich_goal_dict", return_value={"rounds_total": 2, "rounds_passed": 2}):
            note = goals._late_round_note(self._deadline_goal(9), iterations)
        self.assertIsNotNone(note)
        self.assertIn("只剩约 9 分钟，不够再跑一轮（每轮大约 24 分钟）", note)
        self.assertIn("一共跑了 2 轮，其中 2 轮通过检查。", note)

    def test_short_rounds_and_fifteen_minutes_left_continue(self) -> None:
        iterations = [self._round(30, 10), self._round(20, 10)]
        self.assertIsNone(goals._late_round_note(self._deadline_goal(15), iterations))

    def test_under_a_minute_left_wording(self) -> None:
        note = goals._late_round_note(self._deadline_goal(0.3), [], first_round=True)
        self.assertIn("只剩不到 1 分钟", note)

    def test_goal_without_deadline_is_unaffected(self) -> None:
        self.assertIsNone(goals._late_round_note({"id": "g", "deadline_at": None}, [self._round(30, 10)]))

    def test_unparseable_timestamps_are_ignored(self) -> None:
        bad = [{"started_at": "garbage", "finished_at": None}, {"started_at": "", "finished_at": "x"}]
        self.assertIsNone(goals._late_round_note(self._deadline_goal(20), bad))

    def test_finish_report_explains_early_deadline_stop(self) -> None:
        goal = self._create(duration_minutes=30)
        self.clock.advance(minutes=27)
        self._advance()
        done = goals._get_goal_row(self.settings, goal["id"])
        report = goals.build_finish_report_text(self.settings, done)
        self.assertIn("的目标结束了：时间快到了，剩下的时间不够再跑一轮。", report)
