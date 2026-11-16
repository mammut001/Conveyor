"""Unit & HTTP tests for web chat intent detection, execution, and action cards."""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import chat_intents
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_service as svc
from conveyor.core.transcript_store import get_transcript_store
from conveyor.web.web_chat import WebChatPort, build_history
from channel.types import InboundMessage
from tests.test_supervisor_api import TOKEN, _HttpHarness, _make_settings

_GOAL_ROLES = ("analyst", "implementer", "reviewer")


class _AlwaysAvailableExecutor:
    """Stand-in supervisor executor: behaves as if a coding CLI were installed."""

    name = "pool"

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def run(self, **_kwargs: Any) -> Any:
        return ags.ExecutionResult(success=False, output="", error="test stub executor")


def _start_available_executor_patches(test: unittest.TestCase) -> None:
    """Report every supervisor executor as available for the whole test.

    Chat intent flows refuse to start a goal when no executor reports available
    (「目前没有可用的执行者…」), so without these stubs the goal-starting tests
    only pass on machines that happen to have a coding CLI installed. Tests that
    assert the refusal patch their own unavailable state inside the test body;
    those patches start afterwards and therefore still win.
    """
    caps_patch = patch.object(
        chat_intents.svc,
        "get_capabilities",
        return_value={
            "roles": {
                role: {
                    "executor": "pool",
                    "model": None,
                    "location": "local",
                    "available": True,
                    "reason": "",
                    "auto_selected": True,
                }
                for role in _GOAL_ROLES
            },
            "executors": [
                {"name": name, "label": label, "available": True, "reason": ""}
                for name, label in chat_intents.svc._UI_EXPOSED_EXECUTORS
            ],
            "coordinator": {"executor": "pool", "model": None, "available": True},
        },
    )
    caps_patch.start()
    test.addCleanup(caps_patch.stop)

    executor_patch = patch.object(
        chat_intents.ags,
        "get_executor",
        lambda _settings, _name: _AlwaysAvailableExecutor(),
    )
    executor_patch.start()
    test.addCleanup(executor_patch.stop)


class _MemoryPort:
    def __init__(self, settings, session_id: str) -> None:
        self.settings = settings
        self.durable_session_id = session_id
        self.events: list[tuple[str, dict]] = []
        self.prompt = ""
        self._user_persisted = False
        self._delivered_final = False

    def emit(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def _persist_turn(self, msg, text: str, *, kind: str = "chat", metadata=None, final: bool = True) -> None:
        store = get_transcript_store(self.settings)
        ident = dict(channel=msg.channel, operator_id=msg.operator_id, source_chat_id=msg.chat_id)
        if not self._user_persisted:
            store.append(self.durable_session_id, "user", msg.text, kind="chat", **ident)
            self._user_persisted = True
        store.append(self.durable_session_id, "assistant", text, kind=kind, metadata=metadata, **ident)


class DetectIntentTests(unittest.TestCase):
    def test_detect_intent_positives_and_negatives(self) -> None:
        cases: list[tuple[str, str | None, str | None, int | None, str | None]] = [
            # --- create_project positives ---
            ("帮我创建一个 repo 叫 todo-app", "create_project", "todo-app", None, None),
            ("新建一个仓库，名为 billing-ui", "create_project", "billing-ui", None, None),
            ("创建一个叫 portal-web 的项目", "create_project", "portal-web", None, None),
            ("帮我建一个 Git 仓库 叫 demo_1", "create_project", "demo_1", None, None),
            ("新建项目 alpha", "create_project", "alpha", None, None),
            ("开一个新的代码库", "create_project", None, None, None),
            ("新建一个项目", "create_project", None, None, None),
            ("帮我创建一个 repo", "create_project", None, None, None),
            ("create a repo named cool-app", "create_project", "cool-app", None, None),
            ("create a new project called beta-api", "create_project", "beta-api", None, None),
            ("create a github repo", "create_project", None, None, None),
            ("帮我创建一个 GitHub repo 叫 gh-demo", "create_project", "gh-demo", None, None),
            # --- start_timed_goal positives ---
            ("你来迭代十个小时，把首页做好", "start_timed_goal", None, 600, "把首页做好"),
            ("持续改进 3 小时，完善测试", "start_timed_goal", None, 180, "完善测试"),
            ("持续改两个小时：修掉所有警告", "start_timed_goal", None, 120, "修掉所有警告"),
            ("跑半小时", "start_timed_goal", None, 30, ""),
            ("帮我在 todo-app 上干十二个小时，优化性能", "start_timed_goal", "todo-app", 720, "优化性能"),
            ("跑 1.5h，把文档补齐", "start_timed_goal", None, 90, "把文档补齐"),
            ("在 my-app 上迭代一个小时，修好登录", "start_timed_goal", "my-app", 60, "修好登录"),
            ("持续优化两小时半，整理样式", "start_timed_goal", None, 150, "整理样式"),
            ("iterate on todo-app for 4 hours to fix login", "start_timed_goal", "todo-app", 240, "fix login"),
            ("work for 2h", "start_timed_goal", None, 120, ""),
            ("在 repo_1 上跑 45 分钟，跑一遍端到端测试", "start_timed_goal", "repo_1", 45, "跑一遍端到端测试"),
            # --- filler / connector stripping after duration ---
            ("那你来迭代十个小时吧，把它做成一个好用的待办应用", "start_timed_goal", None, 600, "把它做成一个好用的待办应用"),
            ("那你来迭代 2 小时呢，然后把测试补齐吧！", "start_timed_goal", None, 120, "把测试补齐"),
            ("持续改进三小时啊，那么就优化一下性能嘛。", "start_timed_goal", None, 180, "优化一下性能"),
            ("在 todo-app 上跑半小时哈：并且把文档写好！", "start_timed_goal", "todo-app", 30, "把文档写好"),
            ("把它做成一个好用的待办应用，跑十个小时吧", "start_timed_goal", None, 600, "把它做成一个好用的待办应用"),
            ("那你来迭代十个小时吧", "start_timed_goal", None, 600, ""),
            # --- negatives (questions, past tense, negations, general chat) ---
            ("怎么创建 repo？", None, None, None, None),
            ("repo 是什么", None, None, None, None),
            ("迭代十个小时要花多少钱", None, None, None, None),
            ("我昨天迭代了十个小时", None, None, None, None),
            ("我上周创建了一个 repo", None, None, None, None),
            ("刚才建了个项目", None, None, None, None),
            ("不要创建 repo", None, None, None, None),
            ("别跑了，先停一下", None, None, None, None),
            ("how to create a repo?", None, None, None, None),
            ("what is a repository", None, None, None, None),
            ("I iterated for 5 hours yesterday", None, None, None, None),
            ("/goal 把测试修好", None, None, None, None),
            ("今天天气怎么样", None, None, None, None),
        ]
        self.assertGreaterEqual(len(cases), 25)
        for text, exp_kind, exp_proj, exp_dur, exp_goal in cases:
            with self.subTest(text=text):
                got = chat_intents.detect_intent(text)
                if exp_kind is None:
                    self.assertIsNone(got)
                else:
                    self.assertIsNotNone(got)
                    assert got is not None
                    self.assertEqual(got.kind, exp_kind)
                    self.assertEqual(got.project_name, exp_proj)
                    self.assertEqual(got.duration_minutes, exp_dur)
                    if exp_goal is not None:
                        self.assertEqual(got.goal_text, exp_goal)

    def test_github_flag_extracted(self) -> None:
        got = chat_intents.detect_intent("帮我建一个 GitHub repo 叫 my-web")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertTrue(got.mentions_github)
        self.assertEqual(got.project_name, "my-web")

    def test_short_goal_title_first_clause_and_boundaries(self) -> None:
        self.assertEqual(
            chat_intents._short_goal_title("把它做成一个好用的待办应用，支持增删改查和本地存储"),
            "把它做成一个好用的待办应用",
        )
        long_cjk = chat_intents._short_goal_title("把整个项目的首页界面交互体验和响应式布局全部重做一遍，加上测试")
        self.assertLessEqual(len(long_cjk), 24)
        self.assertTrue(long_cjk.endswith("…"))

        word_bound = chat_intents._short_goal_title("给这个项目加上完整的 authentication 和 authorization 机制")
        self.assertEqual(word_bound, "给这个项目加上完整的…")
        self.assertLessEqual(len(word_bound), 24)

        self.assertEqual(
            chat_intents._short_goal_title("fix v1.5 layout, and add dark mode"),
            "fix v1.5 layout",
        )

    def test_classify_intent_fallback_strict_json_and_error_safety(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-ci-cls-") as tmp:
            settings = _make_settings(Path(tmp) / "env")

            async def valid_model(_messages):
                return {
                    "content": json.dumps({
                        "intent": "start_timed_goal",
                        "project_name": "todo-app",
                        "duration_minutes": 180,
                        "goal_text": "整理代码",
                    })
                }

            res = asyncio.run(chat_intents.classify_intent("请帮我给 todo-app 项目持续推进多轮直到天亮", settings, model=valid_model))
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res.kind, "start_timed_goal")
            self.assertEqual(res.duration_minutes, 180)
            self.assertEqual(res.project_name, "todo-app")

            async def bad_json_model(_messages):
                return {"content": "not json"}

            self.assertIsNone(
                asyncio.run(chat_intents.classify_intent("项目要不要持续三小时", settings, model=bad_json_model))
            )

            async def bad_range_model(_messages):
                return {"content": '{"intent":"start_timed_goal","duration_minutes":5,"goal_text":"x"}'}

            self.assertIsNone(
                asyncio.run(chat_intents.classify_intent("项目跑一下小时吧", settings, model=bad_range_model))
            )


class ChatIntentsFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-chat-intents-")
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root / "env")
        self.session_id = "web:web-console:test-intent-session"
        _start_available_executor_patches(self)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _send(self, text: str) -> _MemoryPort:
        port = _MemoryPort(self.settings, self.session_id)
        msg = InboundMessage(
            channel="web",
            operator_id="web-console",
            chat_id="test-intent-session",
            message_id="m1",
            text=text,
            chat_type="p2p",
        )
        handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, text))
        self.assertTrue(handled)
        return port

    def test_create_project_end_to_end_and_duplicate_and_invalid(self) -> None:
        port = self._send("帮我创建一个 repo 叫 todo-app")
        self.assertEqual(len(port.events), 1)
        ev, payload = port.events[0]
        self.assertEqual(ev, "message")
        self.assertIn("已创建项目 todo-app（空的 Git 仓库），可以开始了。", payload["text"])
        card = payload["action_card"]
        labels = [b["label"] for b in card["buttons"]]
        self.assertEqual(labels, ["打开项目", "让它开始干活"])

        repos = svc.list_repositories(self.settings)
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "todo-app")
        managed_wts = [w for w in repos[0]["worktrees"] if w["managed"] and not w["is_main"]]
        self.assertEqual(len(managed_wts), 1)

        # build_history resolves action_card cleanly.
        session = get_transcript_store(self.settings).get_session(self.session_id)
        hist = build_history(session, settings=self.settings)
        assistant_msgs = [m for m in hist if m["role"] == "assistant"]
        self.assertIn("action_card", assistant_msgs[-1])

        # Duplicate name -> says already exists & offers 打开项目.
        dup_port = self._send("帮我创建一个 repo 叫 todo-app")
        _, dup_payload = dup_port.events[0]
        self.assertIn("已经", dup_payload["text"])
        self.assertEqual([b["label"] for b in dup_payload["action_card"]["buttons"]], ["打开项目"])

        # Invalid name -> service's Chinese error.
        bad_port = self._send("帮我创建一个 repo 叫 -bad")
        _, bad_payload = bad_port.events[0]
        self.assertNotIn("action_card", bad_payload)
        self.assertIn("项目名称", bad_payload["text"])

    def test_create_project_pending_followup_and_expiry(self) -> None:
        ask_port = self._send("帮我创建一个 repo")
        self.assertEqual(ask_port.events[0][1]["text"], "项目叫什么名字？（英文字母、数字、短横线）")

        # Next owner message creates the project cleanly.
        follow_port = self._send("my-first-app")
        self.assertIn("已创建项目 my-first-app", follow_port.events[0][1]["text"])
        self.assertEqual(svc.list_repositories(self.settings)[0]["name"], "my-first-app")

        # Expiry after 10 minutes: no longer consumes unrelated next message.
        base_time = 1000.0
        with patch("conveyor.agents.chat_intents.time.time", return_value=base_time):
            self._send("新建一个项目")
        with patch("conveyor.agents.chat_intents.time.time", return_value=base_time + chat_intents.PENDING_TTL_SECONDS + 5):
            port = _MemoryPort(self.settings, self.session_id)
            msg = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="test-intent-session",
                message_id="m2",
                text="not-a-project-anymore",
                chat_type="p2p",
            )
            handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, msg.text))
            self.assertFalse(handled)

    def test_create_project_github_mention_never_calls_github(self) -> None:
        self.settings = replace(self.settings, github_token="ghp_dummy_token_for_test")
        port = self._send("帮我建一个 GitHub repo 叫 hosted-only")
        payload = port.events[0][1]
        self.assertIn("GitHub", payload["text"])
        self.assertEqual(svc.list_repositories(self.settings), [])
        card = payload["action_card"]
        create_btn = card["buttons"][0]
        self.assertEqual(create_btn["action"], "create_hosted_project")
        status, res = chat_intents.execute_action(None, self.settings, create_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(res["ok"])
        self.assertEqual(svc.list_repositories(self.settings)[0]["name"], "hosted-only")

    def test_start_timed_goal_confirmation_creation_and_replay_rejected(self) -> None:
        svc.create_managed_project(self.settings, name="todo-app")
        long_outcome = "把它做成一个好用的待办应用，支持增删改查、标签筛选、本地存储和深色模式"
        port = self._send(f"那你来迭代十个小时吧，{long_outcome}")
        payload = port.events[0][1]
        self.assertIn(f"准备在 todo-app 上持续改进 10 小时：{long_outcome}", payload["text"])
        self.assertIn("时间到或者模型额度用完会自动停。", payload["text"])
        self.assertIn(
            "这个项目还没有自动检查：会让执行者先写一个 scripts/check.sh 跑真正的测试，之后每一轮都用它来验证。",
            payload["text"],
        )
        self.assertNotIn("执行者：", payload["text"])
        card = payload["action_card"]
        self.assertEqual([b["label"] for b in card["buttons"]], ["开始", "每轮等我确认", "改一改", "取消"])

        captured: dict[str, object] = {}
        orig_create_goal = goals_mod.create_goal

        def spy_create_goal(server, settings, body):
            captured.update(body)
            return orig_create_goal(server, settings, body)

        start_btn_id = card["buttons"][0]["id"]
        with patch.object(goals_mod, "create_goal", side_effect=spy_create_goal):
            status, res = chat_intents.execute_action(None, self.settings, start_btn_id)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(captured["duration_minutes"], 600)
        self.assertEqual(captured["mode"], "continuous")
        self.assertEqual(captured["outcome"], long_outcome)
        self.assertEqual(captured["title"], "把它做成一个好用的待办应用")
        goals = goals_mod.list_goals(self.settings)
        self.assertEqual(goals[0]["title"], "把它做成一个好用的待办应用")
        self.assertEqual(goals[0]["outcome"], long_outcome)

        # Backend supports duration_minutes, so the goal has deadline_at; reply uses a relative phrase
        # (never a server-clock HH:MM) while the action card carries the ISO UTC deadline_at.
        follow_text = res["follow_up_message"]["text"]
        self.assertEqual(
            follow_text,
            "已经开始了，大约 10 小时后结束。随时在 项目 › 目标 里看进度或停止。",
        )
        self.assertNotRegex(follow_text, r"\b\d{1,2}:\d{2}\b")
        follow_card = res["follow_up_message"]["action_card"]
        self.assertTrue(follow_card["deadline_at"])
        self.assertEqual(goals[0]["mode"], "continuous")
        self.assertTrue(goals[0]["deadline_at"])

        # Replay of same action_id -> 409 rejected.
        replay_status, replay_body = chat_intents.execute_action(None, self.settings, start_btn_id)
        self.assertEqual(replay_status, HTTPStatus.CONFLICT)
        self.assertEqual(replay_body["error_code"], "action_already_used")

        # Clicking 开始 on a no-checks repo automatically stores ["check-script"].
        from conveyor.agents import supervisor_validation

        repos = svc.list_repositories(self.settings)
        val_state = supervisor_validation.get_repo_validation(self.settings, repos[0]["id"])
        self.assertEqual(val_state["selected"], ["check-script"])
        self.assertEqual(val_state["source"], "repo")

        # Manual, Edit AND Cancel buttons on the same already-used card -> 409 rejected.
        manual_btn_id = card["buttons"][1]["id"]
        manual_status, _ = chat_intents.execute_action(None, self.settings, manual_btn_id)
        self.assertEqual(manual_status, HTTPStatus.CONFLICT)
        edit_btn_id = card["buttons"][2]["id"]
        edit_status, _ = chat_intents.execute_action(None, self.settings, edit_btn_id)
        self.assertEqual(edit_status, HTTPStatus.CONFLICT)
        cancel_btn_id = card["buttons"][3]["id"]
        cancel_status, _ = chat_intents.execute_action(None, self.settings, cancel_btn_id)
        self.assertEqual(cancel_status, HTTPStatus.CONFLICT)

    def test_start_timed_goal_with_deadline_at_and_executor_summary_formats(self) -> None:
        svc.create_managed_project(self.settings, name="todo-app")

        # 1. All roles = pool -> "由执行者池自动安排"
        with patch.object(
            chat_intents.svc,
            "get_capabilities",
            return_value={
                "roles": {
                    "analyst": {"executor": "pool", "available": True},
                    "implementer": {"executor": "pool", "available": True},
                    "reviewer": {"executor": "pool", "available": True},
                }
            },
        ):
            pool_port = self._send("迭代十个小时，把首页做好")
        pool_text = pool_port.events[0][1]["text"]
        self.assertIn("由执行者池自动安排。", pool_text)
        self.assertNotIn("执行者：", pool_text)
        self.assertNotIn("自动轮换", pool_text)

        # 2. Distinct non-pool executors -> listed once cleanly ("用 Pi 和 Claude Code")
        with patch.object(
            chat_intents.svc,
            "get_capabilities",
            return_value={
                "roles": {
                    "analyst": {"executor": "pi", "available": True},
                    "implementer": {"executor": "claude", "available": True},
                    "reviewer": {"executor": "pi", "available": True},
                }
            },
        ):
            mixed_port = self._send("迭代两小时，完善测试")
        mixed_payload = mixed_port.events[0][1]
        self.assertIn("用 Pi 和 Claude Code。", mixed_payload["text"])
        self.assertNotIn("执行者：", mixed_payload["text"])

        # 3. Backend returns deadline_at -> reply stays relative and card carries ISO UTC deadline_at.
        deadline_iso = "2026-11-15T18:30:00Z"
        start_btn_id = mixed_payload["action_card"]["buttons"][0]["id"]
        with patch.object(
            goals_mod,
            "create_goal",
            return_value={
                "id": "goal-deadlined",
                "max_iterations": 20,
                "deadline_at": deadline_iso,
            },
        ):
            status, res = chat_intents.execute_action(None, self.settings, start_btn_id)
        self.assertEqual(status, HTTPStatus.OK)
        follow_text = res["follow_up_message"]["text"]
        self.assertEqual(
            follow_text,
            "已经开始了，大约 2 小时后结束。随时在 项目 › 目标 里看进度或停止。",
        )
        self.assertNotRegex(follow_text, r"\b\d{1,2}:\d{2}\b")
        follow_card = res["follow_up_message"]["action_card"]
        self.assertEqual(follow_card["deadline_at"], "2026-11-15T18:30:00+00:00")
        stored_card = chat_intents.get_action_card(self.settings, follow_card["id"])
        self.assertIsNotNone(stored_card)
        assert stored_card is not None
        self.assertEqual(stored_card["deadline_at"], "2026-11-15T18:30:00+00:00")

    def test_edit_button_claims_card_and_navigation_only_stays_usable(self) -> None:
        # 1. Create project card: 打开项目 (nav-only) + 让它开始干活 (action).
        proj_port = self._send("帮我创建一个 repo 叫 nav-demo")
        proj_card = proj_port.events[0][1]["action_card"]
        open_btn = next(b for b in proj_card["buttons"] if b["action"] == "open_project")
        prefill_btn = next(b for b in proj_card["buttons"] if b["action"] == "prefill_timed_goal")

        # Clicking navigation-only 打开项目 leaves card unused so action button still works.
        nav_status, nav_res = chat_intents.execute_action(None, self.settings, open_btn["id"])
        self.assertEqual(nav_status, HTTPStatus.OK)
        self.assertFalse(nav_res["card"]["used"])

        # Claiming 让它开始干活 marks card used...
        act_status, act_res = chat_intents.execute_action(None, self.settings, prefill_btn["id"])
        self.assertEqual(act_status, HTTPStatus.OK)
        self.assertTrue(act_res["card"]["used"])

        # ...yet navigation-only 打开项目 STILL stays usable afterwards!
        nav_after_status, nav_after_res = chat_intents.execute_action(None, self.settings, open_btn["id"])
        self.assertEqual(nav_after_status, HTTPStatus.OK)
        self.assertTrue(nav_after_res["card"]["used"])

        # 2. Timed goal card: claiming 改一改 marks whole card used -> 开始 and 取消 return 409.
        goal_port = self._send("持续改进 2 小时，整理页面")
        goal_card = goal_port.events[0][1]["action_card"]
        by_label = {b["label"]: b for b in goal_card["buttons"]}
        start_btn = by_label["开始"]
        edit_btn = by_label["改一改"]
        cancel_btn = by_label["取消"]
        edit_status, edit_res = chat_intents.execute_action(None, self.settings, edit_btn["id"])
        self.assertEqual(edit_status, HTTPStatus.OK)
        self.assertTrue(edit_res["card"]["used"])
        self.assertEqual(edit_res["client_action"]["type"], "edit_timed_goal")

        start_after_status, _ = chat_intents.execute_action(None, self.settings, start_btn["id"])
        self.assertEqual(start_after_status, HTTPStatus.CONFLICT)
        cancel_after_status, _ = chat_intents.execute_action(None, self.settings, cancel_btn["id"])
        self.assertEqual(cancel_after_status, HTTPStatus.CONFLICT)

    def test_ambiguous_project_picks_from_buttons_and_executor_refusal(self) -> None:
        svc.create_managed_project(self.settings, name="proj-a")
        svc.create_managed_project(self.settings, name="proj-b")
        # Clear recent_repo_id by using a fresh session.
        self.session_id = "web:web-console:fresh-session"
        port = self._send("持续改进两小时，修好登录")
        payload = port.events[0][1]
        self.assertIn("在哪个项目上干？", payload["text"])
        btn_labels = [b["label"] for b in payload["action_card"]["buttons"]]
        self.assertEqual(set(btn_labels), {"proj-a", "proj-b"})

        # Pick proj-b -> follows up with confirmation card.
        pick_btn = next(b for b in payload["action_card"]["buttons"] if b["label"] == "proj-b")
        status, res = chat_intents.execute_action(None, self.settings, pick_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)
        confirm_card = res["follow_up_message"]["action_card"]
        self.assertIn("proj-b", confirm_card["text"])

        # Simulate executor_check_failed refusal.
        start_btn = confirm_card["buttons"][0]
        with patch.object(
            goals_mod,
            "create_goal",
            side_effect=svc.SupervisorServiceError(
                HTTPStatus.CONFLICT,
                "执行者检查未通过，请先确认执行环境",
                code="executor_check_failed",
            ),
        ):
            r_status, r_res = chat_intents.execute_action(None, self.settings, start_btn["id"])
        self.assertEqual(r_status, HTTPStatus.OK)
        self.assertIn("执行者检查未通过", r_res["follow_up_message"]["text"])
        self.assertEqual(r_res["follow_up_message"]["action_card"]["buttons"][0]["action"], "open_workers")

    def test_timed_goal_relative_phrases_and_no_server_clock_hhmm(self) -> None:
        cases = (
            (30, "大约 30 分钟后结束"),
            (90, "大约 1 小时 30 分后结束"),
            (600, "大约 10 小时后结束"),
            (1440, "大约 24 小时后结束"),
        )
        for minutes, expected_phrase in cases:
            self.assertEqual(chat_intents.format_relative_deadline_zh(minutes), expected_phrase)

        created = svc.create_managed_project(self.settings, name="rel-proj")
        repo_id = created["repository"]["id"]
        for minutes, expected_phrase in cases:
            card = chat_intents.create_action_card(
                self.settings,
                session_id=self.session_id,
                title="确认开始持续改进",
                text="confirm",
                buttons=[
                    {
                        "label": "开始",
                        "action": "confirm_start_timed_goal",
                        "style": "primary",
                        "client_payload": {},
                        "server_payload": {
                            "repo_id": repo_id,
                            "duration_minutes": minutes,
                            "goal_text": f"测试 {minutes} 分钟",
                        },
                    }
                ],
            )
            with patch.object(
                goals_mod,
                "create_goal",
                return_value={
                    "id": f"goal-{minutes}",
                    "max_iterations": 20,
                    "deadline_at": "2026-11-15T18:30:00Z",
                },
            ):
                status, res = chat_intents.execute_action(None, self.settings, card["buttons"][0]["id"])
            self.assertEqual(status, HTTPStatus.OK)
            follow_text = res["follow_up_message"]["text"]
            self.assertEqual(
                follow_text,
                f"已经开始了，{expected_phrase}。随时在 项目 › 目标 里看进度或停止。",
            )
            self.assertNotRegex(follow_text, r"\b\d{1,2}:\d{2}\b")
            self.assertEqual(
                res["follow_up_message"]["action_card"]["deadline_at"],
                "2026-11-15T18:30:00+00:00",
            )

    def test_timed_goal_manual_confirm_leaves_validation_empty(self) -> None:
        from conveyor.agents import supervisor_validation

        created = svc.create_managed_project(self.settings, name="manual-proj")
        repo_id = created["repository"]["id"]
        port = self._send("在 manual-proj 上持续改进 2 小时，整理代码")
        payload = port.events[0][1]
        self.assertIn("scripts/check.sh", payload["text"])
        card = payload["action_card"]
        self.assertEqual([b["label"] for b in card["buttons"]], ["开始", "每轮等我确认", "改一改", "取消"])

        manual_btn = next(b for b in card["buttons"] if b["label"] == "每轮等我确认")
        status, res = chat_intents.execute_action(None, self.settings, manual_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(res["ok"])
        self.assertEqual(len(goals_mod.list_goals(self.settings)), 1)
        val_state = supervisor_validation.get_repo_validation(self.settings, repo_id)
        self.assertEqual(val_state["selected"], [])
        self.assertEqual(val_state["source"], "none")

    def test_timed_goal_repo_with_checks_keeps_unchanged_card(self) -> None:
        from conveyor.agents import supervisor_validation

        created = svc.create_managed_project(self.settings, name="checked-proj")
        repo_id = created["repository"]["id"]
        repo_path = Path(created["repository"]["path"])
        (repo_path / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        supervisor_validation.set_repo_validation(self.settings, repo_id, ["make-test"])

        port = self._send("在 checked-proj 上持续改进 2 小时，修好接口")
        payload = port.events[0][1]
        self.assertNotIn("scripts/check.sh", payload["text"])
        self.assertNotIn("还没有自动检查", payload["text"])
        card = payload["action_card"]
        self.assertEqual([b["label"] for b in card["buttons"]], ["开始", "改一改", "取消"])

        start_btn = card["buttons"][0]
        status, res = chat_intents.execute_action(None, self.settings, start_btn["id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(res["ok"])
        # Existing validation selection must remain intact.
        val_state = supervisor_validation.get_repo_validation(self.settings, repo_id)
        self.assertEqual(val_state["selected"], ["make-test"])


class ChatActionsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        _start_available_executor_patches(self)

    def test_http_chat_actions_executes_server_payload_only_and_rejects_replay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-ci-http-") as tmp:
            settings = _make_settings(Path(tmp) / "env")
            created = svc.create_managed_project(settings, name="web-repo")
            repo_id = created["repository"]["id"]
            card = chat_intents.create_action_card(
                settings,
                session_id="web:web-console:default",
                title="确认开始持续改进",
                text="准备在 web-repo 上持续改进 2 小时：修好页面。",
                buttons=[
                    {
                        "label": "开始",
                        "action": "confirm_start_timed_goal",
                        "style": "primary",
                        "client_payload": {},
                        "server_payload": {
                            "repo_id": repo_id,
                            "duration_minutes": 120,
                            "goal_text": "修好页面",
                        },
                    }
                ],
            )
            btn_id = card["buttons"][0]["id"]
            h = _HttpHarness(settings)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
                conn.request(
                    "POST",
                    "/api/chat/actions",
                    body=json.dumps({
                        "action_id": btn_id,
                        # Client attempts to tamper with duration & repo_id; must be ignored!
                        "duration_minutes": 1440,
                        "repo_id": "forged-repo",
                    }),
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, 200)
                self.assertTrue(data["ok"])
                self.assertTrue(data["card"]["used"])
                goals = goals_mod.list_goals(settings)
                self.assertEqual(len(goals), 1)
                self.assertEqual(goals[0]["repo_id"], repo_id)

                # Replay same action_id -> HTTP 409.
                conn.request(
                    "POST",
                    "/api/chat/actions",
                    body=json.dumps({"action_id": btn_id}),
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )
                resp2 = conn.getresponse()
                resp2.read()
                self.assertEqual(resp2.status, 409)
                conn.close()
            finally:
                svc.stop_server_worker(h.server)
                h.close()


class ChatIntentsFailSafeTests(unittest.TestCase):
    def test_db_path_uses_codex_memory_root_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-ci-dbpath-") as tmp:
            settings = SimpleNamespace(codex_memory_root=Path(tmp))
            db_path = chat_intents._db_path(settings)
            expected_state = Path(tmp).resolve() / "state"
            self.assertEqual(db_path, expected_state / "chat_intents.sqlite3")
            self.assertTrue(expected_state.is_dir())

    def test_has_pending_or_detected_intent_fails_safe_on_db_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-ci-ro1-") as tmp:
            settings = SimpleNamespace(codex_memory_root=Path(tmp))
            with patch.object(
                chat_intents,
                "_connect",
                side_effect=OSError(30, "Read-only file system"),
            ):
                with self.assertLogs(chat_intents.logger, level="ERROR") as logs:
                    self.assertFalse(chat_intents.has_pending_or_detected_intent(settings, "s", "你好"))
                self.assertTrue(any("chat intent state unavailable" in entry for entry in logs.output))
                with self.assertLogs(chat_intents.logger, level="ERROR"):
                    self.assertTrue(
                        chat_intents.has_pending_or_detected_intent(settings, "s", "帮我创建一个 repo 叫 x")
                    )

    def test_maybe_handle_web_chat_returns_false_on_db_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-ci-ro2-") as tmp:
            settings = SimpleNamespace(codex_memory_root=Path(tmp))
            port = _MemoryPort(settings, "web:web-console:s")
            msg = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="s",
                message_id="m1",
                text="你好",
                chat_type="p2p",
            )
            with patch.object(
                chat_intents,
                "_connect",
                side_effect=OSError(30, "Read-only file system"),
            ):
                with self.assertLogs(chat_intents.logger, level="ERROR"):
                    handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, settings, "你好"))
            self.assertFalse(handled)
            self.assertEqual(port.events, [])
