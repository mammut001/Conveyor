"""Hermetic tests for Task F1: goal status labels & finish report handoff."""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch

from channel.types import InboundMessage
from conveyor.agents import agent_supervisor as ags
from conveyor.agents import agents
from conveyor.agents import chat_intents
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_merge as merge_mod
from conveyor.agents import supervisor_service as svc
from conveyor.agents import supervisor_validation
from conveyor.core.transcript_store import get_transcript_store
from conveyor.web.web_chat import build_history
from tests.test_chat_intents import _MemoryPort, _start_available_executor_patches
from tests.test_supervisor_api import _make_settings


def _git_run(cwd: str | Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


class GoalFinishReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-f1-finish-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(goal_notify.drain)
        self.root = Path(self._tmp.name).resolve()
        empty_home = self.root / "home"
        empty_home.mkdir(parents=True, exist_ok=True)
        isolated_env = os.environ.copy()
        for key in (
            "GIT_CONFIG_SYSTEM",
            "GIT_TEMPLATE_DIR",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            isolated_env.pop(key, None)
        isolated_env["HOME"] = str(empty_home)
        isolated_env["GIT_CONFIG_GLOBAL"] = os.devnull
        isolated_env["GIT_CONFIG_NOSYSTEM"] = "1"
        env_patch = patch.dict(os.environ, isolated_env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.settings = _make_settings(self.root / "env")
        self.session_id = "web:web-console:f1-origin-session"
        _start_available_executor_patches(self)

    def _create_project(self, name: str = "qa-todo-cli") -> tuple[dict[str, Any], dict[str, Any]]:
        repo_dir = self.root / "repos" / name
        repo_dir.mkdir(parents=True, exist_ok=True)
        _git_run(repo_dir, ["-c", "init.defaultBranch=main", "init", "-b", "main"])
        _git_run(repo_dir, ["config", "user.email", "test@example.com"])
        _git_run(repo_dir, ["config", "user.name", "Supervisor Test"])
        (repo_dir / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        _git_run(repo_dir, ["add", "-A"])
        _git_run(
            repo_dir,
            [
                "-c", "user.name=Supervisor Test",
                "-c", "user.email=test@example.com",
                "commit", "--no-verify", "-qm", "initial commit",
            ],
        )
        repo, _created = svc.register_repository(self.settings, path=str(repo_dir), name=name)
        wt = svc.create_managed_worktree(self.settings, repo["id"])
        return repo, wt

    def _insert_iteration(
        self,
        goal_id: str,
        n: int,
        *,
        task_id: str = "",
        passed: bool = True,
        round_title: str = "",
        decision: str = "continue",
        task_final_state: str = "completed",
    ) -> None:
        conn = goals_mod._connect(self.settings)
        try:
            conn.execute(
                """INSERT INTO supervisor_goal_iterations
                   (goal_id, n, task_id, prompt, decision, decision_reason,
                    started_at, finished_at, task_final_state, passed, round_title)
                   VALUES (?, ?, ?, 'p', ?, 'ok',
                           '2026-11-16T00:00:00+00:00', '2026-11-16T00:05:00+00:00',
                           ?, ?, ?)""",
                (goal_id, n, task_id, decision, task_final_state, 1 if passed else 0, round_title),
            )
            conn.commit()
        finally:
            conn.close()

    def _commit_in_worktree(self, wt_path: str, filename: str = "todo.py", content: str = "print('ok')\n") -> str:
        target = Path(wt_path) / filename
        target.write_text(content, encoding="utf-8")
        _git_run(wt_path, ["add", filename])
        _git_run(
            wt_path,
            [
                "-c", "user.name=Test",
                "-c", "user.email=test@localhost",
                "commit", "--no-verify", "-m", f"update {filename}",
            ],
        )
        return _git_run(wt_path, ["rev-parse", "HEAD"])

    def test_labels_for_every_state_and_stop_reason_combination(self) -> None:
        cases = [
            ("exhausted", "deadline", "时间到了"),
            ("exhausted", "DEADLINE", "时间到了"),
            ("exhausted", "quota", "额度用完了"),
            ("exhausted", "", "轮数用完了"),
            ("exhausted", "迭代预算已用完（5 轮），目标尚未达成", "轮数用完了"),
            ("achieved", "", "已达成"),
            ("achieved", "用户确认已达成", "已达成"),
            ("stopped", "", "已停止"),
            ("stopped", "用户手动停止了目标", "已停止"),
            ("failed", "模型出错", "没能完成"),
            ("waiting_user", "", "在等你回复"),
            ("waiting_quota", "", "等额度恢复"),
            ("running", "", "进行中"),
        ]
        for state, stop_reason, expected in cases:
            with self.subTest(state=state, stop_reason=stop_reason):
                self.assertEqual(goal_notify.goal_status_label(state, stop_reason), expected)

        # Chat progress reply must use the shared helper and say 「时间到了」, never 「轮数用尽」.
        repo, wt = self._create_project("qa-todo-cli")
        goal = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "完善 CLI",
            "duration_minutes": 60,
            "mode": "continuous",
            "origin_session_id": self.session_id,
        })
        goals_mod._finish_deadline(self.settings, goal, [])
        from conveyor.agents.intent_decider import Decision
        with patch.object(
            chat_intents.intent_decider,
            "decide_intent",
            return_value=Decision("progress", 0.99, {"progress": 0.99}, "gemini", 50),
        ):
            port = _MemoryPort(self.settings, self.session_id)
            msg = InboundMessage(
                channel="web", operator_id="web-console", chat_id="f1-origin-session",
                message_id="m-prog", text="qa-todo-cli 进度怎么样", chat_type="p2p",
            )
            handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, msg.text))
        self.assertTrue(handled)
        reply = port.events[0][1]["text"]
        self.assertIn("qa-todo-cli（时间到了）", reply)
        self.assertNotIn("轮数用尽", reply)

    def test_report_text_for_deadline_quota_achieved_stopped_variants(self) -> None:
        repo, wt = self._create_project("qa-todo-cli")

        # 1) Deadline with 10 passed rounds (caps bullets at 8, newest last), check-script passed, and commits.
        supervisor_validation.set_repo_validation(self.settings, repo["id"], ["check-script"])
        head_sha = self._commit_in_worktree(wt["path"], "cli.py", "def main():\n    return 0\n")
        identity = ags.target_identity(wt["path"])
        fp = identity["fingerprint"]

        start_dt = datetime(2026, 11, 16, 0, 0, 0, tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(hours=1, minutes=15)
        with patch.object(goals_mod, "_now", return_value=start_dt):
            g_deadline = goals_mod.create_goal(None, self.settings, {
                "repo_id": repo["id"],
                "worktree_id": wt["id"],
                "outcome": "完善 CLI 工具",
                "duration_minutes": 60,
                "mode": "continuous",
                "origin_session_id": self.session_id,
            })
        for idx in range(1, 11):
            is_pass = idx != 2
            self._insert_iteration(
                g_deadline["id"],
                idx,
                task_id=f"task-d-{idx}",
                passed=is_pass,
                round_title=f"第{idx}项功能" if is_pass else "",
                decision="continue",
                task_final_state="completed" if is_pass else "failed",
            )
        # Last round has check-script passed in acceptance table
        task_10 = ags.create_task(self.settings, goal="last", repo_path=repo["path"], worktree_path=wt["path"], base_sha=head_sha)
        conn = goals_mod._connect(self.settings)
        try:
            conn.execute(
                "UPDATE supervisor_goal_iterations SET task_id = ? WHERE goal_id = ? AND n = 10",
                (task_10["id"], g_deadline["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        ags.record_acceptance(
            self.settings,
            task_10["id"],
            passed=True,
            sha=head_sha,
            fingerprint=fp,
            executed_checks=[{"name": "check-script", "argv": ["sh", "scripts/check.sh"], "passed": True}],
            evidence_source="operator_validation+independent_review",
            provenance_verified=True,
        )

        with patch.object(goals_mod, "_now", return_value=end_dt):
            finished = goals_mod._finish_deadline(self.settings, g_deadline)

        report_deadline = goals_mod.build_finish_report_text(self.settings, finished)
        lines = report_deadline.splitlines()
        self.assertEqual(lines[0], "qa-todo-cli 的目标结束了：时间到了。")
        self.assertEqual(lines[1], "跑了 10 轮，通过 9 轮，用时 1 小时 15 分。")
        bullets = [ln for ln in lines if ln.startswith("- ")]
        self.assertEqual(len(bullets), 8)
        self.assertEqual(bullets[0], "- 第3项功能")
        self.assertEqual(bullets[-1], "- 第10项功能")
        self.assertIn("自动检查：scripts/check.sh（最后一轮 通过）", lines)
        self.assertIn(f"改动在工作目录 {wt['branch']}，还没有合并到主分支。", lines)

        # 2) Check-script failed on last round
        ags.record_acceptance(
            self.settings,
            task_10["id"],
            passed=False,
            sha=head_sha,
            fingerprint=fp,
            executed_checks=[{"name": "check-script", "argv": ["sh", "scripts/check.sh"], "passed": False}],
            evidence_source="operator_validation+independent_review",
            provenance_verified=False,
        )
        report_fail_check = goals_mod.build_finish_report_text(self.settings, finished)
        self.assertIn("自动检查：scripts/check.sh（最后一轮 没通过）", report_fail_check)

        # 3) Without check-script and without commits (fresh repo/worktree)
        repo2, wt2 = self._create_project("clean-proj")
        with patch.object(goals_mod, "_now", return_value=start_dt):
            g_quota = goals_mod.create_goal(None, self.settings, {
                "repo_id": repo2["id"],
                "worktree_id": wt2["id"],
                "outcome": "修复测试",
                "duration_minutes": 60,
                "mode": "continuous",
            })
        with patch.object(goals_mod, "_now", return_value=start_dt + timedelta(minutes=25)):
            g_quota = goals_mod._finish_goal(
                self.settings,
                g_quota,
                goals_mod.GOAL_EXHAUSTED,
                summary="额度用完了",
                stop_reason=goals_mod.STOP_REASON_QUOTA,
            )
        report_quota = goals_mod.build_finish_report_text(self.settings, g_quota)
        self.assertIn("clean-proj 的目标结束了：额度用完了。", report_quota)
        self.assertIn("跑了 0 轮，通过 0 轮，用时 25 分。", report_quota)
        self.assertNotIn("scripts/check.sh", report_quota)
        self.assertIn("没有产生改动。", report_quota)

        # 4) Achieved & Stopped
        g_ach = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo2["id"],
            "worktree_id": wt2["id"],
            "outcome": "达成目标",
        })
        g_ach = goals_mod._finish_goal(self.settings, g_ach, goals_mod.GOAL_ACHIEVED, summary="ok")
        self.assertIn("clean-proj 的目标结束了：已达成。", goals_mod.build_finish_report_text(self.settings, g_ach))

        g_stop = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo2["id"],
            "worktree_id": wt2["id"],
            "outcome": "停下来",
        })
        g_stop = goals_mod.stop_goal(None, self.settings, g_stop["id"])
        self.assertIn("clean-proj 的目标结束了：已停止。", goals_mod.build_finish_report_text(self.settings, g_stop))

        # 5) goal_notify uses the same report + web chat hint, without buttons
        notify_text = goal_notify.notification_text(self.settings, finished, goals_mod.GOAL_EXHAUSTED, "telegram")
        self.assertIn("qa-todo-cli 的目标结束了：时间到了。", notify_text)
        self.assertIn("在网页 对话 里可以合并或再跑", notify_text)
        self.assertNotIn("confirm_merge", notify_text)

    def test_finish_report_idempotency_across_terminal_transitions_and_restart(self) -> None:
        repo, wt = self._create_project("idem-proj")
        goal = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "幂等验证",
            "origin_session_id": self.session_id,
        })
        goals_mod._finish_goal(self.settings, goal, goals_mod.GOAL_ACHIEVED, summary="done")
        # Second terminal transition (and after module reload simulating restart)
        goals_mod._finish_goal(self.settings, goal, goals_mod.GOAL_EXHAUSTED, summary="again")
        importlib.reload(goals_mod)
        goals_mod._on_goal_terminal(self.settings, goals_mod._get_goal_row(self.settings, goal["id"]))

        session = get_transcript_store(self.settings).get_session(self.session_id)
        hist = build_history(session, settings=self.settings)
        report_msgs = [m for m in hist if m["role"] == "assistant" and "的目标结束了：" in m["text"]]
        self.assertEqual(len(report_msgs), 1)
        self.assertEqual(
            [b["label"] for b in report_msgs[0]["action_card"]["buttons"]],
            ["查看改动", "合并到主分支", "再跑 1 小时"],
        )

    def test_origin_session_stored_migrated_and_fallback_used(self) -> None:
        repo, wt = self._create_project("origin-proj")

        # 1) Created via chat_intents -> stores origin_session_id and posts report there
        port = _MemoryPort(self.settings, self.session_id)
        msg = InboundMessage(
            channel="web", operator_id="web-console", chat_id="f1-origin-session",
            message_id="m-start", text="在 origin-proj 上持续改进 1 小时：补测试", chat_type="p2p",
        )
        handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, msg.text))
        self.assertTrue(handled)
        confirm_card = port.events[0][1]["action_card"]
        start_btn = next(b for b in confirm_card["buttons"] if b["action"] == "confirm_start_timed_goal")
        status, res = chat_intents.execute_action(None, self.settings, start_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)

        created_goals = goals_mod.list_goals(self.settings)
        self.assertEqual(len(created_goals), 1)
        self.assertEqual(created_goals[0]["origin_session_id"], self.session_id)

        goals_mod.stop_goal(None, self.settings, created_goals[0]["id"])
        origin_session = get_transcript_store(self.settings).get_session(self.session_id)
        origin_hist = build_history(origin_session, settings=self.settings)
        self.assertTrue(any("origin-proj 的目标结束了：已停止。" in m["text"] for m in origin_hist))

        # 2) Created without origin_session_id (目标 tab) -> falls back to default agent main web-chat session
        g_tab = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "从目标页创建",
        })
        self.assertIsNone(g_tab["origin_session_id"])
        goals_mod._finish_goal(self.settings, g_tab, goals_mod.GOAL_ACHIEVED, summary="完成")

        default_sid = agents.session_id_for(agents.DEFAULT_AGENT_ID)
        default_session = get_transcript_store(self.settings).get_session(default_sid)
        default_hist = build_history(default_session, settings=self.settings)
        self.assertTrue(any("origin-proj 的目标结束了：已达成。" in m["text"] for m in default_hist))

        # 3) Legacy table without origin_session_id migrates cleanly
        legacy_settings = _make_settings(self.root / "legacy-env")
        db_path = goals_mod._db_path(legacy_settings)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """CREATE TABLE supervisor_goals (
                       id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, worktree_id TEXT NOT NULL,
                       title TEXT NOT NULL, outcome TEXT NOT NULL, max_iterations INTEGER NOT NULL DEFAULT 5,
                       role_overrides_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       question TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                       stop_reason TEXT NOT NULL DEFAULT '', last_next_prompt TEXT NOT NULL DEFAULT ''
                   )"""
            )
            conn.commit()
        finally:
            conn.close()
        goals_mod._INITIALIZED_DBS.clear()
        migrated_conn = goals_mod._connect(legacy_settings)
        try:
            cols = {r["name"] for r in migrated_conn.execute("PRAGMA table_info(supervisor_goals)").fetchall()}
            self.assertIn("origin_session_id", cols)
        finally:
            migrated_conn.close()

    def test_merge_confirmation_calls_merge_worktree_and_shows_errors_and_rerun(self) -> None:
        repo, wt = self._create_project("merge-proj")
        self._commit_in_worktree(wt["path"], "feature.txt", "hello\n")

        goal = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "完成 feature.txt",
            "origin_session_id": self.session_id,
        })
        goals_mod._finish_goal(self.settings, goal, goals_mod.GOAL_ACHIEVED, summary="ok")

        session = get_transcript_store(self.settings).get_session(self.session_id)
        hist = build_history(session, settings=self.settings)
        report_card = next(m["action_card"] for m in hist if m.get("action_card"))
        btns = {b["label"]: b for b in report_card["buttons"]}
        self.assertEqual(set(btns.keys()), {"查看改动", "合并到主分支", "再跑 1 小时"})

        # 查看改动 is navigation-only and leaves card unused
        nav_status, nav_res = chat_intents.execute_action(None, self.settings, btns["查看改动"]["id"])
        self.assertEqual(nav_status, HTTPStatus.OK)
        self.assertFalse(nav_res["card"]["used"])
        self.assertEqual(nav_res["client_action"]["payload"]["tab"], "workspace")
        self.assertEqual(nav_res["client_action"]["payload"]["worktree_id"], wt["id"])

        # 合并到主分支 opens confirmation card saying 合并后不能自动撤销
        req_status, req_res = chat_intents.execute_action(None, self.settings, btns["合并到主分支"]["id"])
        self.assertEqual(req_status, HTTPStatus.OK)
        confirm_card = req_res["follow_up_message"]["action_card"]
        self.assertIn(f"把 {wt['branch']} 合并到 main？", confirm_card["text"])
        self.assertIn("合并后不能自动撤销", confirm_card["text"])
        confirm_btn = next(b for b in confirm_card["buttons"] if b["label"] == "确认合并")

        # Simulate merge_worktree returning an error first (via mock to verify error surfacing)
        with patch.object(
            merge_mod,
            "merge_worktree",
            side_effect=merge_mod.SupervisorMergeError(
                HTTPStatus.CONFLICT,
                "主工作目录当前不在 main 分支上，请先切回去再合并",
                code=merge_mod.CODE_TARGET_NOT_CHECKED_OUT,
            ),
        ):
            # Create a fresh confirmation card to test error path without burning the real merge
            _txt, err_confirm = chat_intents._build_merge_confirmation_card(
                self.settings,
                self.session_id,
                worktree_id=wt["id"],
                repo_id=repo["id"],
                source_branch=wt["branch"],
                target_branch="main",
            )
            err_btn = next(b for b in err_confirm["buttons"] if b["label"] == "确认合并")
            err_status, err_res = chat_intents.execute_action(None, self.settings, err_btn["id"])
        self.assertEqual(err_status, HTTPStatus.OK)
        self.assertIn("主工作目录当前不在 main 分支上", err_res["follow_up_message"]["text"])

        # Real merge_worktree call succeeds
        with patch.object(merge_mod, "merge_worktree", wraps=merge_mod.merge_worktree) as wrapped_merge:
            m_status, m_res = chat_intents.execute_action(None, self.settings, confirm_btn["id"])
        self.assertEqual(m_status, HTTPStatus.OK)
        wrapped_merge.assert_called_once_with(self.settings, wt["id"], {"confirm": True})
        self.assertIn(f"已经把 {wt['branch']} 合并到 main", m_res["follow_up_message"]["text"])
        self.assertTrue((Path(repo["path"]) / "feature.txt").is_file())

        # 再跑 1 小时 on a fresh report card creates a new continuous 60-minute goal on the same worktree
        goal2 = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "继续打磨 CLI",
            "origin_session_id": self.session_id,
        })
        goals_mod.stop_goal(None, self.settings, goal2["id"])
        session2 = get_transcript_store(self.settings).get_session(self.session_id)
        hist2 = build_history(session2, settings=self.settings)
        card2 = [m["action_card"] for m in hist2 if m.get("action_card") and "继续打磨" not in m["text"]][-1]
        rerun_btn = next(b for b in card2["buttons"] if b["label"] == "再跑 1 小时")
        before_ids = {g["id"] for g in goals_mod.list_goals(self.settings)}
        r_status, r_res = chat_intents.execute_action(None, self.settings, rerun_btn["id"])
        self.assertEqual(r_status, HTTPStatus.OK)
        all_goals = goals_mod.list_goals(self.settings)
        self.assertEqual(len(all_goals), len(before_ids) + 1)
        newest = next(g for g in all_goals if g["id"] not in before_ids)
        self.assertEqual(newest["outcome"], "继续打磨 CLI")
        self.assertEqual(newest["worktree_id"], wt["id"])
        self.assertEqual(newest["mode"], "continuous")
        self.assertIsNotNone(newest["deadline_at"])
        self.assertEqual(newest["origin_session_id"], self.session_id)
        self.assertEqual(r_res["follow_up_message"]["action_card"]["title"], "已开始持续改进")

    def test_stop_from_chat_persists_action_reply_before_finish_report(self) -> None:
        from conveyor.agents.intent_decider import Decision

        repo, wt = self._create_project("qa-todo-cli")
        supervisor_validation.set_repo_validation(self.settings, repo["id"], ["check-script"])
        goal = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "完善 CLI",
            "duration_minutes": 60,
            "mode": "continuous",
            "origin_session_id": self.session_id,
        })
        task = ags.create_task(
            self.settings,
            goal="第 1 轮",
            repo_path=repo["path"],
            worktree_path=wt["path"],
        )
        ags.transition(self.settings, task["id"], "implementing", stage="implement")
        self._insert_iteration(
            goal["id"],
            1,
            task_id=task["id"],
            passed=False,
            decision="",
            task_final_state="",
        )

        with patch.object(
            chat_intents.intent_decider,
            "decide_intent",
            return_value=Decision("stop", 0.95, {"stop": 0.95}, "gemini", 40),
        ):
            port = _MemoryPort(self.settings, self.session_id)
            msg = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="f1-origin-session",
                message_id="m-stop",
                text="先停一下",
                chat_type="p2p",
            )
            handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, msg.text))
        self.assertTrue(handled)
        stop_card = port.events[0][1]["action_card"]
        stop_btn = next(b for b in stop_card["buttons"] if b["label"] == "停止")

        status, res = chat_intents.execute_action(None, self.settings, stop_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(
            res["follow_up_message"]["text"],
            "已停止 qa-todo-cli 的目标，已经做好的改动会保留。",
        )

        session = get_transcript_store(self.settings).get_session(self.session_id)
        hist = build_history(session, settings=self.settings)
        assistant_texts = [m["text"] for m in hist if m["role"] == "assistant"]
        reply_idx = next(
            i for i, txt in enumerate(assistant_texts)
            if txt == "已停止 qa-todo-cli 的目标，已经做好的改动会保留。"
        )
        report_idx = next(
            i for i, txt in enumerate(assistant_texts)
            if "qa-todo-cli 的目标结束了：已停止。" in txt
        )
        self.assertLess(reply_idx, report_idx)
        report_text = assistant_texts[report_idx]
        self.assertIn("自动检查：scripts/check.sh（最后一轮没有跑到检查就停了）", report_text)
        self.assertNotIn("最后一轮 没通过", report_text)

        # Idempotency: calling post_finish_report / _on_goal_terminal again does not duplicate.
        goals_mod.post_finish_report(self.settings, goals_mod.get_goal(self.settings, goal["id"]))
        hist_after = build_history(
            get_transcript_store(self.settings).get_session(self.session_id),
            settings=self.settings,
        )
        reports = [m for m in hist_after if m["role"] == "assistant" and "的目标结束了：" in m["text"]]
        self.assertEqual(len(reports), 1)

    def test_check_script_wording_all_four_outcomes(self) -> None:
        repo, wt = self._create_project("check-wording-proj")
        supervisor_validation.set_repo_validation(self.settings, repo["id"], ["check-script"])
        head_sha = _git_run(wt["path"], ["rev-parse", "HEAD"])
        fp = ags.target_identity(wt["path"])["fingerprint"]

        # 1) Never validated (0 rounds ran)
        g_never = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "还没跑检查",
        })
        g_never = goals_mod.stop_goal(None, self.settings, g_never["id"])
        txt_never = goals_mod.build_finish_report_text(self.settings, g_never)
        self.assertIn("自动检查：scripts/check.sh（还没有跑过检查）", txt_never)

        # 2) Cancelled before validation on round 1 (stored validation absent)
        g_cancel1 = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "第一轮被停",
        })
        t1 = ags.create_task(
            self.settings, goal="r1", repo_path=repo["path"], worktree_path=wt["path"], base_sha=head_sha,
        )
        ags.transition(self.settings, t1["id"], "cancelled", stage="cancelled", error="stopped")
        self._insert_iteration(
            g_cancel1["id"], 1, task_id=t1["id"], passed=False, decision="", task_final_state="cancelled",
        )
        g_cancel1 = goals_mod.stop_goal(None, self.settings, g_cancel1["id"])
        txt_cancel1 = goals_mod.build_finish_report_text(self.settings, g_cancel1)
        self.assertIn("自动检查：scripts/check.sh（最后一轮没有跑到检查就停了）", txt_cancel1)
        self.assertNotIn("最后一轮 没通过", txt_cancel1)

        # 3) Passed on last round (stored check-script passed=True)
        g_pass = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "检查通过",
        })
        t_pass = ags.create_task(
            self.settings, goal="pass", repo_path=repo["path"], worktree_path=wt["path"], base_sha=head_sha,
        )
        ags.record_acceptance(
            self.settings,
            t_pass["id"],
            passed=True,
            sha=head_sha,
            fingerprint=fp,
            executed_checks=[{"name": "check-script", "argv": ["sh", "scripts/check.sh"], "passed": True}],
            evidence_source="operator_validation+independent_review",
            provenance_verified=True,
        )
        self._insert_iteration(
            g_pass["id"], 1, task_id=t_pass["id"], passed=True, decision="achieved", task_final_state="completed",
        )
        g_pass = goals_mod._finish_goal(self.settings, g_pass, goals_mod.GOAL_ACHIEVED, summary="ok")
        self.assertIn(
            "自动检查：scripts/check.sh（最后一轮 通过）",
            goals_mod.build_finish_report_text(self.settings, g_pass),
        )

        # 4) Failed on last round (stored check-script passed=False)
        ags.record_acceptance(
            self.settings,
            t_pass["id"],
            passed=False,
            sha=head_sha,
            fingerprint=fp,
            executed_checks=[{"name": "check-script", "argv": ["sh", "scripts/check.sh"], "passed": False}],
            evidence_source="operator_validation+independent_review",
            provenance_verified=False,
        )
        self.assertIn(
            "自动检查：scripts/check.sh（最后一轮 没通过）",
            goals_mod.build_finish_report_text(self.settings, g_pass),
        )

        # 5) Round 1 passed validation, Round 2 cancelled before validation -> does not leak Round 1 result;
        #    even if iteration row claims passed=True / completed, missing stored validation -> not_run.
        g_two = goals_mod.create_goal(None, self.settings, {
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
            "outcome": "第二轮没跑到检查",
        })
        t_r1 = ags.create_task(
            self.settings, goal="r1", repo_path=repo["path"], worktree_path=wt["path"], base_sha=head_sha,
        )
        ags.record_acceptance(
            self.settings,
            t_r1["id"],
            passed=True,
            sha=head_sha,
            fingerprint=fp,
            executed_checks=[{"name": "check-script", "argv": ["sh", "scripts/check.sh"], "passed": True}],
            evidence_source="operator_validation+independent_review",
            provenance_verified=True,
        )
        t_r2 = ags.create_task(
            self.settings, goal="r2", repo_path=repo["path"], worktree_path=wt["path"], base_sha=head_sha,
        )
        self._insert_iteration(
            g_two["id"], 1, task_id=t_r1["id"], passed=True, decision="continue", task_final_state="completed",
        )
        self._insert_iteration(
            g_two["id"], 2, task_id=t_r2["id"], passed=True, decision="", task_final_state="completed",
        )
        g_two = goals_mod.stop_goal(None, self.settings, g_two["id"])
        txt_two = goals_mod.build_finish_report_text(self.settings, g_two)
        self.assertIn("自动检查：scripts/check.sh（最后一轮没有跑到检查就停了）", txt_two)
        self.assertNotIn("最后一轮 通过", txt_two)


if __name__ == "__main__":
    unittest.main()
