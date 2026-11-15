"""tests/test_supervisor_goals_timeboxed.py — Time-boxed & continuous goals tests (Task A1).

Covers: duration validation, deadline enforcement (never start after deadline,
let running round finish), continuous mode prompts + round tracking, 3 consecutive
failures pause, quota pause/auto-resume/deadline-exceeded stop, startup recovery,
goal dict metrics, and schema migration from older DBs.
"""
from __future__ import annotations

import asyncio
import json
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
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
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


class TimeboxedGoalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-timebox-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
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
        self.assertEqual(paused["question"], goals.CONTINUOUS_FAILURES_QUESTION)
        self.assertEqual(paused["consecutive_failures"], 3)
        self.assertEqual(len(paused["iterations"]), 3)

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


if __name__ == "__main__":
    unittest.main()
