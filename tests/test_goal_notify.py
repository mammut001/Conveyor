"""tests/test_goal_notify.py — 目标通知（goal notifications）unit tests.

Covers the owner-facing Chinese text for every notified state, the restart-safe
dedupe, the Web Push payload (which must never carry the goal text), the shared
Telegram/Feishu helpers (patched, never the network), the off switch, a failing
sender, and the `supervisor_goals` call sites. No network, no model, no browser.
"""
from __future__ import annotations

import importlib
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import goal_notify
import supervisor_goals as goals
import web_push
from config import load_runtime_settings

GOAL_ID = "goal-0123456789abcdef"
QUESTION = "这一轮的改动已经做完，请检查结果后告诉我是否达成，或者还要改什么。"
STOP_REASON = "第 2 轮任务派发失败：worktree 已经被别的任务占用"

PUSH_SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/goal-endpoint",
    "keys": {"p256dh": "A" * 86, "auth": "B" * 22},
}


def _drain(timeout: float = 10.0) -> None:
    """Wait until the goal-notify worker has processed everything queued so far."""
    if not goal_notify.drain(timeout):
        raise AssertionError("goal notification worker did not drain in time")


def _goal(**overrides: object) -> dict:
    row = {
        "id": GOAL_ID,
        "repo_id": "repo-1",
        "worktree_id": "wt-1",
        "title": "修好登录页的报错",
        "outcome": "修好登录页的报错，并保证测试通过",
        "max_iterations": 5,
        "state": "waiting_user",
        "question": QUESTION,
        "summary": "第一轮改动已完成。",
        "stop_reason": "",
        "iterations_used": 3,
        "updated_at": "2024-05-01T10:00:00+00:00",
    }
    row.update(overrides)
    return row


class GoalNotifyTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-notify-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = SimpleNamespace(
            codex_memory_root=self.root / "memory",
            telegram_bot_token="tg-token",
            telegram_allowed_user_id=42,
            lark_app_id="lark-app",
            lark_app_secret="lark-secret",
            lark_allowed_open_id="ou-owner",
            pwa_enabled=False,
            goal_notify_enabled=True,
        )

    def tearDown(self) -> None:
        # The notify worker writes into this temp state dir; let it finish
        # before the directory is removed in the cleanups.
        goal_notify.drain()

    # ------------------------------------------------------------------
    # one Chinese message per state
    # ------------------------------------------------------------------

    def test_waiting_user_asks_the_owner_to_reply(self) -> None:
        text = goal_notify.notification_text(self.settings, _goal(), "waiting_user")
        self.assertEqual(
            text,
            "目标「修好登录页的报错」需要你回复：" + QUESTION + "\n\n打开 Conveyor 的「项目」页查看。",
        )

    def test_achieved_reports_the_round_number(self) -> None:
        text = goal_notify.notification_text(self.settings, _goal(), "achieved")
        self.assertEqual(
            text, "目标「修好登录页的报错」已达成（第 3 轮）。\n\n打开 Conveyor 的「项目」页查看。"
        )

    def test_exhausted_reports_the_budget(self) -> None:
        text = goal_notify.notification_text(self.settings, _goal(), "exhausted")
        self.assertEqual(
            text, "目标「修好登录页的报错」用完了 5 轮还没达成。\n\n打开 Conveyor 的「项目」页查看。"
        )

    def test_failed_reports_the_reason(self) -> None:
        goal = _goal(state="failed", stop_reason=STOP_REASON)
        text = goal_notify.notification_text(self.settings, goal, "failed")
        self.assertEqual(
            text, "目标「修好登录页的报错」没能完成：" + STOP_REASON + "\n\n打开 Conveyor 的「项目」页查看。"
        )

    def test_question_and_reason_are_clipped_to_200_characters(self) -> None:
        long_question = "请" * 400
        text = goal_notify.notification_text(self.settings, _goal(question=long_question), "waiting_user")
        self.assertIn("请" * goal_notify.MAX_DETAIL_CHARS + "…", text)
        self.assertNotIn("请" * (goal_notify.MAX_DETAIL_CHARS + 1), text)

        long_reason = "因" * 400
        failed = goal_notify.notification_text(
            self.settings, _goal(state="failed", stop_reason=long_reason), "failed"
        )
        self.assertIn("因" * goal_notify.MAX_DETAIL_CHARS + "…", failed)

    def test_only_the_four_states_get_a_message(self) -> None:
        for state in ("running", "stopped", "", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(goal_notify.notification_text(self.settings, _goal(), state), "")

    def test_every_message_is_simplified_chinese_and_ends_with_the_hint(self) -> None:
        for state in goal_notify.NOTIFY_STATES:
            with self.subTest(state=state):
                goal = _goal(state=state, stop_reason=STOP_REASON)
                text = goal_notify.notification_text(self.settings, goal, state)
                self.assertTrue(text)
                self.assertTrue(text.endswith(goal_notify.OPEN_HINT))
                self.assertIn("目标", text)
                self.assertNotIn("[truncated]", text)
                self.assertNotIn("\n\n\n", text)


class GoalNotifyDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-notify-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = SimpleNamespace(
            codex_memory_root=self.root / "memory",
            telegram_bot_token="tg-token",
            telegram_allowed_user_id=42,
            lark_app_id="lark-app",
            lark_app_secret="lark-secret",
            lark_allowed_open_id="ou-owner",
            pwa_enabled=False,
            goal_notify_enabled=True,
        )
        self.telegram: list[tuple[object, str]] = []
        self.feishu: list[tuple[object, str]] = []
        self.pushed: list[tuple[object, str, str]] = []
        self._channel_originals: dict[str, Any] = {}
        self._patch_channels()

    def tearDown(self) -> None:
        # The notify worker writes into this temp state dir; let it finish
        # before the directory is removed in the cleanups.
        goal_notify.drain()

    def _patch_channels(self) -> None:
        """(Re)install the fake channels; a module reload undoes the earlier ones."""
        fakes = {
            "_deliver_telegram": lambda settings, goal_id, text: self.telegram.append((settings, text)),
            "_deliver_feishu": lambda settings, goal_id, text: self.feishu.append((settings, text)),
            "_deliver_push": lambda settings, goal_id, kind: self.pushed.append((settings, goal_id, kind)),
        }
        for name, fake in fakes.items():
            # Remember the real channel function so one test can hand it back.
            self._channel_originals.setdefault(name, getattr(goal_notify, name))
            patcher = patch.object(goal_notify, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_channel(self, name: str) -> None:
        """Let the real channel function run for this test (fake stays patched over).

        A fresh patcher installs the original on top of the fixture's fake, so every
        patcher is still started and stopped exactly once.
        """
        original = self._channel_originals.get(name)
        if original is None:  # pragma: no cover - defensive
            return
        patcher = patch.object(goal_notify, name, original)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_public_dns(self) -> None:
        """Offline tests must not depend on public DNS availability."""
        dns = patch.object(web_push.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.202", 443)),
        ])
        dns.start()
        self.addCleanup(dns.stop)

    def _notify(self, goal: dict | None = None, state: str = "waiting_user") -> None:
        goal_notify.notify_goal_change(self.settings, goal or _goal(), state)
        _drain()

    # ------------------------------------------------------------------
    # channels
    # ------------------------------------------------------------------

    def test_telegram_and_feishu_get_the_full_chinese_text(self) -> None:
        self._notify()
        expected = goal_notify.notification_text(self.settings, _goal(), "waiting_user")
        self.assertEqual([text for _settings, text in self.telegram], [expected])
        self.assertEqual([text for _settings, text in self.feishu], [expected])
        self.assertIn(QUESTION, self.telegram[0][1])

    def test_push_uses_the_goal_kind_and_only_the_goal_id(self) -> None:
        # Hand the push channel back to the real function, then capture the actual
        # `web_push.queue_notification` call: kind "goal" and nothing but the id.
        self._restore_channel("_deliver_push")
        pushed: list[tuple[object, str, str]] = []
        with patch.object(web_push, "queue_notification",
                          side_effect=lambda settings, item_id, kind: pushed.append((settings, item_id, kind))):
            self._notify()
        self.assertEqual(len(pushed), 1)
        _settings, item_id, kind = pushed[0]
        self.assertEqual(item_id, GOAL_ID)
        self.assertEqual(kind, "goal")
        captured = " ".join(str(part) for part in pushed[0])
        self.assertNotIn(QUESTION, captured)
        self.assertNotIn(_goal()["title"], captured)

    def test_push_payload_carries_only_the_goal_id_and_a_chinese_title(self) -> None:
        # The real web_push payload: nothing but an opaque id and a generic title,
        # because the browser vendor stores and renders push payloads.
        settings = SimpleNamespace(
            pwa_enabled=True,
            codex_memory_root=self.root / "push",
            operator_name="operator-A",
        )
        self._fake_public_dns()
        (self.root / "push").mkdir(parents=True, exist_ok=True)
        web_push.subscribe(settings, PUSH_SUB)
        outbound = []
        sent = web_push.notify_pending(
            settings, GOAL_ID, "goal", sender=lambda subscription, message: outbound.append(message)
        )
        self.assertEqual(sent, 1)
        payload = json.loads(outbound[0])
        self.assertEqual(payload["id"], GOAL_ID)
        self.assertEqual(payload["kind"], "goal")
        self.assertEqual(payload["title"], "Conveyor：目标需要你看一下")
        for forbidden in ("question", "stop_reason", "summary", "title_text", "修好登录页的报错", QUESTION):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("修好登录页的报错", outbound[0])
        self.assertNotIn(QUESTION, outbound[0])

    def test_approval_and_draft_payloads_are_unchanged(self) -> None:
        settings = SimpleNamespace(
            pwa_enabled=True,
            codex_memory_root=self.root / "push",
            operator_name="operator-A",
        )
        self._fake_public_dns()
        web_push.subscribe(settings, PUSH_SUB)
        outbound = []
        web_push.notify_pending(
            settings, "draft123", "draft", sender=lambda _subscription, message: outbound.append(message)
        )
        self.assertEqual(json.loads(outbound[0]), {"title": "Conveyor needs your review", "id": "draft123"})

    def test_telegram_and_feishu_helpers_are_the_shared_sentry_ones(self) -> None:
        """The goal channels reuse the helpers `deliver_sentry_alerts` uses."""
        # Hand both channels back to the real functions: they are what must reach
        # the (patched) sentry helpers instead of the class-level fakes.
        self._restore_channel("_deliver_telegram")
        self._restore_channel("_deliver_feishu")
        sentry_calls: list[tuple[str, str]] = []
        with patch("personal_tools.sentry.send_owner_telegram",
                   side_effect=lambda settings, text, **_kw: sentry_calls.append(("telegram", text))), \
             patch("personal_tools.sentry.send_owner_feishu",
                   side_effect=lambda settings, text, **_kw: sentry_calls.append(("feishu", text))):
            self._notify()
        self.assertEqual([channel for channel, _text in sentry_calls], ["telegram", "feishu"])
        self.assertEqual(len({text for _channel, text in sentry_calls}), 1)
        expected = goal_notify.notification_text(self.settings, _goal(), "waiting_user")
        self.assertEqual([text for _channel, text in sentry_calls], [expected, expected])

    # ------------------------------------------------------------------
    # dedupe
    # ------------------------------------------------------------------

    def test_a_second_call_and_a_restart_never_notify_twice(self) -> None:
        self._notify()
        self._notify()
        self.assertEqual(len(self.telegram), 1)
        self.assertEqual(len(self.feishu), 1)
        self.assertEqual(len(self.pushed), 1)

        # A "restart": fresh module state (new queue, no worker yet, no memory).
        importlib.reload(goal_notify)
        self._patch_channels()
        goal_notify.notify_goal_change(self.settings, _goal(), "waiting_user")
        _drain()
        self.assertEqual(len(self.telegram), 1)

        # A genuinely new transition (new updated_at) is delivered again.
        goal_notify.notify_goal_change(self.settings, _goal(updated_at="2024-05-01T11:00:00+00:00"),
                                       "waiting_user")
        _drain()
        self.assertEqual(len(self.telegram), 2)

    def test_dedupe_table_is_bounded(self) -> None:
        from supervisor_goals import _connect
        for index in range(goal_notify.MAX_DEDUPE_ROWS + 20):
            goal_notify.notify_goal_change(
                self.settings, _goal(updated_at=f"2024-05-01T10:{index // 60:02d}:{index % 60:02d}+00:00"),
                "waiting_user",
            )
        _drain()
        conn = _connect(self.settings)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM supervisor_goal_notifications").fetchone()[0]
        finally:
            conn.close()
        self.assertLessEqual(rows, goal_notify.MAX_DEDUPE_ROWS)

    def test_unreadable_storage_stays_quiet(self) -> None:
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        broken = SimpleNamespace(codex_memory_root=blocker / "state", goal_notify_enabled=True)
        goal_notify.notify_goal_change(broken, _goal(), "waiting_user")
        _drain()
        self.assertEqual(self.telegram, [])

    # ------------------------------------------------------------------
    # off switch / robustness
    # ------------------------------------------------------------------

    def test_off_switch_queues_nothing(self) -> None:
        self.settings.goal_notify_enabled = False
        goal_notify.notify_goal_change(self.settings, _goal(), "waiting_user")
        self.assertEqual(goal_notify._QUEUE.qsize(), 0)
        self.assertEqual(self.telegram, [])
        self.assertEqual(self.feishu, [])

    def test_stopped_never_notifies(self) -> None:
        self._notify(_goal(state="stopped"), "stopped")
        self.assertEqual(self.telegram, [])
        self.assertEqual(self.feishu, [])
        self.assertEqual(self.pushed, [])

    def test_failing_sender_does_not_propagate_or_stop_the_others(self) -> None:
        def boom(settings, _goal_id, _text):
            raise RuntimeError("telegram transport exploded")

        with patch.object(goal_notify, "_deliver_telegram", boom):
            self.assertIsNone(goal_notify.notify_goal_change(self.settings, _goal(), "waiting_user"))
            _drain()
        self.assertEqual(self.telegram, [])
        self.assertEqual(len(self.feishu), 1)
        self.assertEqual(len(self.pushed), 1)

    def test_broken_goal_payload_is_ignored(self) -> None:
        for goal in (None, {}, {"title": "没有 id"}, "not-a-dict"):
            with self.subTest(goal=goal):
                self.assertIsNone(goal_notify.notify_goal_change(self.settings, goal, "waiting_user"))
        _drain()
        self.assertEqual(self.telegram, [])

    def test_env_flag_defaults_on_and_turns_off(self) -> None:
        env = {
            "CODEX_WORKSPACE_ROOT": str(self.root / "ws"),
            "CODEX_TASK_ROOT": str(self.root / "tasks"),
            "CODEX_MEMORY_ROOT": str(self.root / "memory"),
            "TELEGRAM_ALLOWED_USER_ID": "1",
        }
        (self.root / "ws").mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(load_runtime_settings(env_file="").goal_notify_enabled)
        with patch.dict(os.environ, {**env, "CONVEYOR_GOAL_NOTIFY": "false"}, clear=True):
            self.assertFalse(load_runtime_settings(env_file="").goal_notify_enabled)


class GoalNotifySupervisorTests(unittest.TestCase):
    """`supervisor_goals` transitions must notify exactly once, `stopped` never."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-notify-sup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = SimpleNamespace(
            codex_memory_root=self.root,
            agent_supervisor_enabled=True,
            goal_notify_enabled=True,
        )
        self.notified: list[tuple[object, dict, str]] = []
        patcher = patch.object(goals, "notify_goal_change",
                               lambda settings, goal, state: self.notified.append((settings, goal, state)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.goal_id = self._insert_goal(state=goals.GOAL_WAITING_USER)

    def tearDown(self) -> None:
        # The notify worker writes into this temp state dir; let it finish
        # before the directory is removed in the cleanups.
        goal_notify.drain()

    def _insert_goal(self, state: str) -> str:
        goal_id = f"{goals.GOAL_ID_PREFIX}{os.urandom(4).hex()}"
        now = "2024-05-01T10:00:00+00:00"
        conn = goals._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_goals
                       (id, repo_id, worktree_id, title, outcome, max_iterations,
                        role_overrides_json, state, created_at, updated_at,
                        question, summary, stop_reason, last_next_prompt)
                       VALUES (?, 'repo-1', 'wt-1', '修好登录页的报错', '修好登录页的报错',
                               5, '{}', ?, ?, ?, '这一轮做完了，请看看', '摘要', '', '')""",
                    (goal_id, state, now, now),
                )
        finally:
            conn.close()
        return goal_id

    def test_finish_goal_notifies_once_per_transition(self) -> None:
        goal = goals._get_goal_row(self.settings, self.goal_id)
        goals._finish_goal(self.settings, goal, goals.GOAL_WAITING_USER,
                           summary="摘要", question="这一轮做完了，请看看")
        self.assertEqual([state for _s, _g, state in self.notified], ["waiting_user"])
        _settings, updated, state = self.notified[0]
        self.assertEqual(updated["state"], "waiting_user")
        self.assertEqual(updated["question"], "这一轮做完了，请看看")

    def test_reply_achieved_notifies_once(self) -> None:
        goals.reply_goal(None, self.settings, self.goal_id, {"achieved": True})
        self.assertEqual([state for _s, _g, state in self.notified], ["achieved"])
        self.assertEqual(self.notified[0][1]["stop_reason"], "用户确认已达成")

    def test_stop_goal_never_notifies(self) -> None:
        goals.stop_goal(None, self.settings, self.goal_id)
        self.assertEqual(self.notified, [])

    def test_failed_goal_notifies(self) -> None:
        goal = goals._get_goal_row(self.settings, self.goal_id)
        goals._fail_goal(self.settings, goal, "第 1 轮任务派发失败：worktree 冲突")
        self.assertEqual([state for _s, _g, state in self.notified], ["failed"])
        self.assertEqual(self.notified[0][1]["stop_reason"], "第 1 轮任务派发失败：worktree 冲突")

    def test_notifications_table_is_created_with_the_goals_schema(self) -> None:
        conn = goals._connect(self.settings)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
        finally:
            conn.close()
        self.assertIn("supervisor_goal_notifications", tables)


if __name__ == "__main__":
    unittest.main()
