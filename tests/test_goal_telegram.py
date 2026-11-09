"""tests/test_goal_telegram.py — 从手机上回复等待中的目标（Telegram / Feishu）。

Covers the four commands `handlers/goal_commands.py` registers in
`COMMAND_TABLE` (`/goals`, `/goal_reply`, `/goal_done`, `/goal_stop`):
list formatting, short-id resolution, the reply/done/stop transitions, the
owner-only + private-only gate, the goal-loop wake hook (no web server handle),
and the one-line `/goal_reply` hint in the `waiting_user` notification.

No network, no model, no browser: a disposable state root plus the real
`supervisor_goals` SQLite layer.
"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import goal_notify
import supervisor_goals as goals
import supervisor_service as svc
from channel.types import InboundMessage
from handlers import goal_commands as gc
from handlers.commands import COMMAND_TABLE, run_command

GOAL_ID = "goal-0a1b2c3d4e5f6070"
SHORT_ID = "0a1b2c"
QUESTION = "这一轮的改动已经做完，但没有配置自动验证命令，请检查结果后告诉我是否达成或还要改什么"


def _settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        codex_memory_root=root,
        agent_supervisor_enabled=True,
        telegram_allowed_user_id="1",
        lark_allowed_open_id="ou-owner",
        goal_notify_enabled=True,
    )


def _goal_row(**overrides: Any) -> dict:
    row = {
        "id": GOAL_ID,
        "title": "修好登录页的报错",
        "state": "waiting_user",
        "created_at": "2024-05-01T10:00:00+00:00",
        "updated_at": "2024-05-01T10:00:00+00:00",
        "iterations_used": 2,
        "max_iterations": 5,
        "question": QUESTION,
    }
    row.update(overrides)
    return row


class _Port:
    """Minimal OutboundPort: records every reply."""

    supports_inline_buttons = False
    supports_attachments = False

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "placeholder"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "fresh"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.replies.append(text)
        return True


def _msg(text: str, *, channel: str = "telegram", chat_type: str = "p2p",
         operator: str | None = None, chat_id: str = "1") -> InboundMessage:
    if operator is None:
        operator = "1" if channel == "telegram" else "ou-owner"
    return InboundMessage(
        channel=channel,  # type: ignore[arg-type]
        operator_id=operator,
        chat_id=chat_id,
        message_id="m1",
        text=text,
        chat_type=chat_type,  # type: ignore[arg-type]
    )


class _GoalCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: one `waiting_user` goal in a disposable goals DB."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-tg-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.settings = _settings(self.root)
        self.notified: list[tuple[Any, dict, str]] = []
        patcher = patch.object(
            goals, "notify_goal_change",
            lambda settings, goal, state: self.notified.append((settings, goal, state)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.goal_id = self._insert_goal("waiting_user", GOAL_ID, rounds=2)

    # -- helpers ---------------------------------------------------------

    def _insert_goal(self, state: str, goal_id: str, *, title: str = "修好登录页的报错",
                     created_at: str = "2024-05-01T10:00:00+00:00",
                     rounds: int = 0) -> str:
        now = "2024-05-01T10:00:00+00:00"
        conn = goals._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_goals
                       (id, repo_id, worktree_id, title, outcome, max_iterations,
                        role_overrides_json, state, created_at, updated_at,
                        question, summary, stop_reason, last_next_prompt)
                       VALUES (?, 'repo-1', 'wt-1', ?, '修好登录页的报错，并保证测试通过',
                               5, '{}', ?, ?, ?, ?, '摘要', '', '')""",
                    (goal_id, title, state, created_at, now, QUESTION),
                )
                for n in range(1, rounds + 1):
                    conn.execute(
                        """INSERT INTO supervisor_goal_iterations
                           (goal_id, n, task_id, prompt, decision, decision_reason,
                            started_at, finished_at, task_final_state, evaluating, evaluating_since)
                           VALUES (?, ?, '', ?, 'ask_user', '需要人工确认', ?, '', 'waiting_user', 0, '')""",
                        (goal_id, n, f"这是目标「{title}」的第 {n} 轮。", now),
                    )
        finally:
            conn.close()
        return goal_id

    async def _command(self, name: str, arg: str, msg: InboundMessage | None = None) -> _Port:
        port = _Port()
        handled = await run_command(name, msg or _msg(f"/{name} {arg}"), port, None,
                                    self.settings, arg)
        self.assertTrue(handled, f"/{name} must be registered in COMMAND_TABLE")
        return port

    def _goal(self, goal_id: str) -> dict:
        return goals.get_goal(self.settings, goal_id)


# ---------------------------------------------------------------------------
# 1. /goals list formatting
# ---------------------------------------------------------------------------

class GoalListTextTests(unittest.TestCase):
    def test_empty_list_says_so_in_chinese(self) -> None:
        self.assertEqual(gc.goals_text([]), "当前没有需要你处理的目标。")
        self.assertEqual(gc.goals_text([_goal_row(state="achieved")]), "当前没有需要你处理的目标。")

    def test_only_waiting_user_and_running_are_listed(self) -> None:
        for state in ("achieved", "stopped", "exhausted", "failed"):
            with self.subTest(state=state):
                self.assertEqual(gc.goals_text([_goal_row(state=state)]), "当前没有需要你处理的目标。")

    def test_line_shows_title_chinese_state_rounds_and_short_id(self) -> None:
        text = gc.goals_text([_goal_row()])
        line = text.splitlines()[1]
        self.assertEqual(
            line,
            f"1. 「修好登录页的报错」等待你回复 · 第 2 / 5 轮 · 短编号 {SHORT_ID}",
        )
        self.assertIn("回复等待中的目标：/goal_reply <短编号> <你的回答>", text)
        self.assertIn("确认达成：/goal_done <短编号>", text)
        self.assertIn("停止：/goal_stop <短编号>", text)

    def test_running_state_label_and_no_rounds_yet(self) -> None:
        text = gc.goals_text([_goal_row(state="running", iterations_used=0)])
        self.assertIn("「修好登录页的报错」进行中 · 还没开始", text)

    def test_waiting_user_comes_first_even_when_it_is_older(self) -> None:
        text = gc.goals_text([
            _goal_row(id="goal-aaaaaa00000000", state="running",
                      created_at="2024-05-02T10:00:00+00:00"),
            _goal_row(id="goal-bbbbbb00000000", state="waiting_user",
                      created_at="2024-05-01T10:00:00+00:00"),
        ])
        lines = [line for line in text.splitlines() if ". 「" in line]
        self.assertTrue(lines[0].startswith("1. 「修好登录页的报错」等待你回复"))
        self.assertTrue(lines[1].startswith("2. 「修好登录页的报错」进行中"))

    def test_at_most_ten_goals_are_listed(self) -> None:
        many = [
            _goal_row(id=f"goal-{index:012x}", state="running",
                      created_at=f"2024-05-01T10:{index:02d}:00+00:00")
            for index in range(12)
        ]
        text = gc.goals_text(many)
        lines = [line for line in text.splitlines() if ". 「" in line]
        self.assertEqual(len(lines), gc.MAX_LISTED_GOALS)
        self.assertIn("还有 2 个目标没显示出来。", text)
        self.assertIn("目标（10 个，等待你回复的在前）：", text)

    def test_commands_are_registered_for_both_channels(self) -> None:
        for name in ("goals", "goal_reply", "goal_done", "goal_stop"):
            with self.subTest(command=name):
                self.assertIn(name, COMMAND_TABLE)
        self.assertIs(COMMAND_TABLE["goals"].handler, gc._goals)
        self.assertIs(COMMAND_TABLE["goal_reply"].handler, gc._goal_reply)
        self.assertIs(COMMAND_TABLE["goal_done"].handler, gc._goal_done)
        self.assertIs(COMMAND_TABLE["goal_stop"].handler, gc._goal_stop)


# ---------------------------------------------------------------------------
# 2. short-id resolution
# ---------------------------------------------------------------------------

class ShortGoalIdTests(_GoalCase):
    def test_short_id_is_the_first_six_characters_after_the_prefix(self) -> None:
        self.assertEqual(goals.short_goal_id(GOAL_ID), SHORT_ID)
        self.assertEqual(goals.short_goal_id(SHORT_ID), SHORT_ID)
        self.assertEqual(goals.short_goal_id(""), "")
        self.assertEqual(goals.short_goal_id(None), "")

    def test_short_id_resolves_to_the_full_goal_id(self) -> None:
        self.assertEqual(goals.resolve_goal_id(self.settings, SHORT_ID), GOAL_ID)

    def test_full_id_still_resolves(self) -> None:
        self.assertEqual(goals.resolve_goal_id(self.settings, GOAL_ID), GOAL_ID)

    def test_unknown_short_id_is_a_chinese_error(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as caught:
            goals.resolve_goal_id(self.settings, "zzzzzz")
        self.assertIn("找不到目标：zzzzzz", str(caught.exception.message))
        self.assertIn("/goals", str(caught.exception.message))

    def test_empty_token_is_a_chinese_error(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as caught:
            goals.resolve_goal_id(self.settings, "   ")
        self.assertIn("短编号", str(caught.exception.message))

    def test_ambiguous_short_id_is_refused_in_chinese(self) -> None:
        self._insert_goal("running", "goal-ff00ff00000000aa")
        self._insert_goal("running", "goal-ff00ff00000000bb")
        with self.assertRaises(svc.SupervisorServiceError) as caught:
            goals.resolve_goal_id(self.settings, "ff00ff")
        message = str(caught.exception.message)
        self.assertIn("ff00ff", message)
        self.assertIn("2 个目标", message)
        self.assertIn("完整目标 ID", message)


# ---------------------------------------------------------------------------
# 3. reply / done / stop transitions
# ---------------------------------------------------------------------------

class GoalCommandTransitionTests(_GoalCase):
    async def test_goals_lists_the_waiting_goal_from_the_phone(self) -> None:
        port = await self._command("goals", "")
        self.assertEqual(len(port.replies), 1)
        text = port.replies[0]
        self.assertIn("「修好登录页的报错」等待你回复 · 第 2 / 5 轮", text)
        self.assertIn(f"短编号 {SHORT_ID}", text)

    async def test_reply_starts_the_next_iteration_with_the_owners_message(self) -> None:
        port = await self._command("goal_reply", f"{SHORT_ID} 请把单元测试补上")
        goal = self._goal(self.goal_id)
        self.assertEqual(goal["state"], "running")
        self.assertEqual(goal["question"], "")
        self.assertEqual(len(goal["iterations"]), 3)
        latest = goal["iterations"][-1]
        self.assertEqual(latest["n"], 3)
        self.assertIn("请把单元测试补上", latest["prompt"])
        self.assertIn("第 3 轮", latest["prompt"])
        self.assertIn("目标「修好登录页的报错」继续跑（第 3 / 5 轮）", port.replies[-1])

    async def test_reply_without_a_message_shows_the_chinese_usage(self) -> None:
        for arg in ("", SHORT_ID):
            with self.subTest(arg=arg):
                port = await self._command("goal_reply", arg)
                self.assertEqual(port.replies, ["用法：/goal_reply <短编号> <你的回答>"])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_done_confirms_the_goal_and_notifies_once(self) -> None:
        port = await self._command("goal_done", SHORT_ID)
        goal = self._goal(self.goal_id)
        self.assertEqual(goal["state"], "achieved")
        self.assertEqual(goal["stop_reason"], "用户确认已达成")
        self.assertIn("已确认达成", port.replies[-1])
        self.assertEqual([state for _s, _g, state in self.notified], ["achieved"])

    async def test_done_without_a_short_id_shows_the_chinese_usage(self) -> None:
        port = await self._command("goal_done", "")
        self.assertEqual(port.replies, ["用法：/goal_done <短编号>"])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_stop_stops_the_goal_and_never_notifies(self) -> None:
        port = await self._command("goal_stop", SHORT_ID)
        goal = self._goal(self.goal_id)
        self.assertEqual(goal["state"], "stopped")
        self.assertEqual(goal["stop_reason"], "用户手动停止了目标")
        self.assertIn("已停止", port.replies[-1])
        self.assertEqual(self.notified, [])

    async def test_stop_without_a_short_id_shows_the_chinese_usage(self) -> None:
        port = await self._command("goal_stop", "")
        self.assertEqual(port.replies, ["用法：/goal_stop <短编号>"])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_unknown_short_id_is_reported_in_chinese(self) -> None:
        for name, arg in (("goal_reply", "zzzzzz 继续"), ("goal_done", "zzzzzz"),
                          ("goal_stop", "zzzzzz")):
            with self.subTest(command=name):
                port = await self._command(name, arg)
                self.assertEqual(len(port.replies), 1)
                self.assertIn("找不到目标：zzzzzz", port.replies[0])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_ambiguous_short_id_is_reported_in_chinese(self) -> None:
        self._insert_goal("running", "goal-ff00ff00000000aa")
        self._insert_goal("running", "goal-ff00ff00000000bb")
        port = await self._command("goal_stop", "ff00ff")
        self.assertIn("对应 2 个目标", port.replies[-1])
        self.assertEqual(self._goal("goal-ff00ff00000000aa")["state"], "running")

    async def test_replying_to_a_goal_that_is_not_waiting_is_a_chinese_conflict(self) -> None:
        goals.stop_goal(None, self.settings, self.goal_id)
        port = await self._command("goal_reply", f"{SHORT_ID} 再试试")
        self.assertIn("目标当前没有在等待用户回复", port.replies[-1])

    async def test_stopping_an_already_finished_goal_says_so(self) -> None:
        await self._command("goal_done", SHORT_ID)
        port = await self._command("goal_stop", SHORT_ID)
        self.assertIn("已经达成了，不用停", port.replies[-1])
        self.assertEqual(self._goal(self.goal_id)["state"], "achieved")

    async def test_feishu_owner_uses_the_same_commands(self) -> None:
        port = await self._command("goal_done", SHORT_ID,
                                   msg=_msg("/goal_done " + SHORT_ID, channel="feishu"))
        self.assertEqual(self._goal(self.goal_id)["state"], "achieved")
        self.assertIn("已确认达成", port.replies[-1])

    async def test_a_failing_action_never_raises_into_the_dispatcher(self) -> None:
        with patch.object(goals, "reply_goal", side_effect=RuntimeError("boom")):
            port = await self._command("goal_reply", f"{SHORT_ID} 继续")
        self.assertEqual(port.replies, ["这个操作没成功，请稍后再试。"])


# ---------------------------------------------------------------------------
# 4. owner-only + private-only gate
# ---------------------------------------------------------------------------

class GoalCommandGateTests(_GoalCase):
    async def test_group_chat_is_refused_and_changes_nothing(self) -> None:
        for name, arg in (("goals", ""), ("goal_reply", f"{SHORT_ID} 继续"),
                          ("goal_done", SHORT_ID), ("goal_stop", SHORT_ID)):
            with self.subTest(command=name):
                port = await self._command(name, arg, msg=_msg(f"/{name}", chat_type="group"))
                self.assertEqual(len(port.replies), 1)
                self.assertIn("私聊", port.replies[0])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")
        self.assertEqual(self.notified, [])

    async def test_a_telegram_forum_topic_counts_as_a_group(self) -> None:
        # An owner's Forum Topic routes like a DM (`owner_topic`), but `chat_type`
        # keeps the real group type, so the private-only guard must still refuse.
        msg = replace(_msg("/goal_stop " + SHORT_ID, chat_type="group"), owner_topic=True)
        port = await self._command("goal_stop", SHORT_ID, msg=msg)
        self.assertIn("私聊", port.replies[0])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_another_operator_is_refused_and_changes_nothing(self) -> None:
        for name, arg in (("goals", ""), ("goal_reply", f"{SHORT_ID} 继续"),
                          ("goal_done", SHORT_ID), ("goal_stop", SHORT_ID)):
            with self.subTest(command=name):
                port = await self._command(name, arg, msg=_msg(f"/{name}", operator="999"))
                self.assertEqual(len(port.replies), 1)
                self.assertIn("老板本人", port.replies[0])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_an_unconfigured_feishu_operator_is_refused(self) -> None:
        self.settings.lark_allowed_open_id = ""
        port = await self._command("goal_stop", SHORT_ID,
                                   msg=_msg("/goal_stop", channel="feishu"))
        self.assertIn("老板本人", port.replies[0])
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_web_channel_is_refused(self) -> None:
        port = await self._command("goal_stop", SHORT_ID,
                                   msg=_msg("/goal_stop", channel="web"))
        self.assertEqual(len(port.replies), 1)
        self.assertEqual(self._goal(self.goal_id)["state"], "waiting_user")

    async def test_owner_in_a_private_chat_is_allowed(self) -> None:
        port = await self._command("goals", "", msg=_msg("/goals"))
        self.assertIn("等待你回复", port.replies[0])


# ---------------------------------------------------------------------------
# 5. the goal loop wakes up without a web server handle
# ---------------------------------------------------------------------------

class GoalWakeHookTests(_GoalCase):
    def _hook(self, seen: list[Any]):
        def hook(settings: Any) -> None:
            seen.append(settings)

        off = goals.set_goal_wake_hook(hook)
        self.assertIsNotNone(off)
        self.addCleanup(off)
        return hook

    def test_reply_without_a_server_wakes_the_goal_loop(self) -> None:
        seen: list[Any] = []
        self._hook(seen)
        goals.reply_goal(None, self.settings, self.goal_id, {"message": "把测试补上"})
        self.assertEqual(seen, [self.settings])

    def test_done_without_a_server_wakes_the_goal_loop(self) -> None:
        seen: list[Any] = []
        self._hook(seen)
        goals.reply_goal(None, self.settings, self.goal_id, {"achieved": True})
        self.assertEqual(seen, [self.settings])

    def test_stop_without_a_server_wakes_the_goal_loop(self) -> None:
        seen: list[Any] = []
        self._hook(seen)
        goals.stop_goal(None, self.settings, self.goal_id)
        self.assertEqual(seen, [self.settings])

    def test_stopping_an_already_finished_goal_wakes_nothing(self) -> None:
        seen: list[Any] = []
        self._hook(seen)
        goals.stop_goal(None, self.settings, self.goal_id)
        goals.stop_goal(None, self.settings, self.goal_id)
        self.assertEqual(seen, [self.settings])

    def test_a_failing_hook_never_breaks_the_reply(self) -> None:
        def boom(_settings: Any) -> None:
            raise RuntimeError("no loop here")

        off = goals.set_goal_wake_hook(boom)
        self.addCleanup(off)
        replied = goals.reply_goal(None, self.settings, self.goal_id, {"message": "继续"})
        self.assertEqual(replied["state"], "running")

    def test_hooks_are_unregistered(self) -> None:
        seen: list[Any] = []
        hook = self._hook(seen)
        self.assertIn(hook, goals.wake_hooks())
        # Registering the same hook twice must not call it twice per wake.
        goals.set_goal_wake_hook(hook)
        self.assertEqual(len([h for h in goals.wake_hooks() if h is hook]), 1)
        goals.wake_goal_loop(self.settings)
        self.assertEqual(seen, [self.settings])
        off = goals.set_goal_wake_hook(hook)
        self.assertIsNotNone(off)
        off()
        self.assertNotIn(hook, goals.wake_hooks())
        goals.wake_goal_loop(self.settings)
        self.assertEqual(seen, [self.settings])

    def test_worker_manager_registers_and_unregisters_the_hook(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        worker = svc.SupervisorWorkerManager(loop)
        worker.ensure_started(self.settings)
        self.assertTrue(
            any(getattr(hook, "__self__", None) is worker for hook in goals.wake_hooks()),
            "SupervisorWorkerManager must register a wake hook on start",
        )
        worker.stop()
        self.assertFalse(
            any(getattr(hook, "__self__", None) is worker for hook in goals.wake_hooks()),
            "SupervisorWorkerManager must unregister its wake hook on stop",
        )

    def test_the_registered_hook_wakes_the_running_worker_loop(self) -> None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True, name="goal-wake")
        thread.start()
        worker = svc.SupervisorWorkerManager(loop)
        try:
            worker.ensure_started(self.settings)
            self.assertIsNotNone(worker._wake_event)

            async def _wait_wake() -> bool:
                event = worker._wake_event
                if event is None:  # pragma: no cover - defensive
                    return False
                return await asyncio.wait_for(event.wait(), timeout=3.0)

            future = asyncio.run_coroutine_threadsafe(_wait_wake(), loop)
            time.sleep(0.05)
            goals.wake_goal_loop(self.settings)
            self.assertTrue(future.result(timeout=5.0))
        finally:
            worker.stop()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=3.0)
            loop.close()


# ---------------------------------------------------------------------------
# 6. the /goal_reply hint in the waiting_user notification
# ---------------------------------------------------------------------------

class GoalNotificationHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-hint-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        self.telegram: list[str] = []
        self.feishu: list[str] = []
        self.pushed: list[tuple[Any, str, str]] = []

        def telegram(settings: Any, goal_id: str, text: str) -> None:
            self.telegram.append(text)

        def feishu(settings: Any, goal_id: str, text: str) -> None:
            self.feishu.append(text)

        def push(settings: Any, goal_id: str, kind: str) -> None:
            self.pushed.append((settings, goal_id, kind))

        for name, fake in (("_deliver_telegram", telegram),
                           ("_deliver_feishu", feishu)):
            patcher = patch.object(goal_notify, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Keep the real `_deliver_push` so the test proves it never forwards text.
        import web_push
        patcher = patch.object(web_push, "queue_notification", push)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        goal_notify.drain()

    def _notify(self) -> None:
        goal_notify.notify_goal_change(self.settings, _goal_row(), "waiting_user")
        self.assertTrue(goal_notify.drain(10.0), "notification worker did not drain")

    def test_telegram_message_ends_with_the_reply_hint(self) -> None:
        self._notify()
        self.assertEqual(len(self.telegram), 1)
        hint = goal_notify.TELEGRAM_REPLY_HINT.format(short_id=SHORT_ID)
        self.assertTrue(self.telegram[0].endswith(hint), self.telegram[0])
        self.assertTrue(self.telegram[0].endswith(goal_notify.OPEN_HINT + "\n\n" + hint))

    def test_feishu_message_carries_the_feishu_hint_and_never_mentions_telegram(self) -> None:
        self._notify()
        self.assertEqual(len(self.feishu), 1)
        hint = goal_notify.FEISHU_REPLY_HINT.format(short_id=SHORT_ID)
        self.assertIn(hint, self.feishu[0])
        self.assertNotIn("Telegram", self.feishu[0])

    def test_other_states_carry_no_reply_hint(self) -> None:
        for state in ("achieved", "exhausted", "failed"):
            with self.subTest(state=state):
                text = goal_notify.notification_text(
                    self.settings, _goal_row(state=state, stop_reason="原因"), state, "telegram")
                self.assertTrue(text.endswith(goal_notify.OPEN_HINT))
                self.assertNotIn("goal_reply", text)

    def test_the_push_channel_never_gets_the_text(self) -> None:
        self._notify()
        # Web Push only ever receives (settings, goal id, kind) — no text at all.
        self.assertEqual(len(self.pushed), 1)
        _settings, goal_id, kind = self.pushed[0]
        self.assertEqual((goal_id, kind), (GOAL_ID, "goal"))

    def test_notification_text_defaults_to_no_hint(self) -> None:
        text = goal_notify.notification_text(self.settings, _goal_row(), "waiting_user")
        self.assertTrue(text.endswith(goal_notify.OPEN_HINT))
        self.assertNotIn("goal_reply", text)

    def test_a_goal_without_a_usable_id_gets_no_hint(self) -> None:
        text = goal_notify.notification_text(
            self.settings, _goal_row(id=""), "waiting_user", "telegram")
        self.assertTrue(text.endswith(goal_notify.OPEN_HINT))
        self.assertEqual(goal_notify.reply_hint("", "telegram"), "")
        self.assertEqual(goal_notify.reply_hint(GOAL_ID, ""), "")
        self.assertEqual(goal_notify.reply_hint(GOAL_ID, "web"), "")


if __name__ == "__main__":
    unittest.main()
