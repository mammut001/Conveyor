"""Comprehensive tests for Telegram agent delegation UX presentation boundaries.

Covers:
- Confirmation human labels and unchanged approval payload
- Fail-closed target validation for confirmation
- Queue ack success vs refusal formatting (never saying running when queued)
- Backend enqueue contract preservation (literal 'agent task queued: <uuid>')
- Real execute_confirmed integration (Telegram buttons vs fallback vs Feishu unchanged)
- Owner-scoped status queries across all lifecycle states:
  queued, running (leased), retrying vs terminal failure on attempts cap exhaustion,
  expired past deadline, generic stopped (dead)
- Outbox delivery state inspection (sent, pending/retry with refresh button, dead, missing)
- Historical completed tasks preserved under archived agent
- Kill switch fails closed without touching SQLite
- Security gates: SQL prefix scoping before LIMIT, wildcard rejection,
  wrong agent mapping refusal, cross-chat/operator/channel/group denial, forged callbacks
- Simple equal-width Markdown table conversion to natural bullet pairs (• 项目：住房；金额（元）：1800)
- Separator column count mismatch, escaped pipes, code-span pipes, and code fences preserved untouched
- Telegram result notification format with localized header, stable [result:...] tag, and bounded length
- Bot command (/agentstatus) and callback handler (anchored regex) integration with isolated environment
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Provide fallback environment variables before importing bot or config
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/dummy_codex_ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/dummy_codex_tasks")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/dummy_codex_mem")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy_token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")

import agent_context as ac
import agent_delegation as delegation
import agent_delegation_queue as queue
import agent_result_outbox as outbox
import agent_telegram_presentation as tg_pres
import config
from config import Settings
import main_router as router
from agents import AgentStore, chat_id_for, WEB_OPERATOR
from channel.types import InboundMessage, OutboundPort
from handlers.tools.confirm import create_pending, get_pending
from handlers.tools.runner import _request_confirmation, execute_confirmed

# Ensure config.load_settings for initial bot import uses isolated dummy env, no real .env.
# Preserve previous module settings/runner state rather than leave per-test temp roots.
if "bot" in sys.modules:
    import bot
else:
    with patch.dict(os.environ, {
        "CONVEYOR_ENV_FILE": os.devnull,
        "CODEX_WORKSPACE_ROOT": "/tmp/dummy_codex_ws",
        "CODEX_TASK_ROOT": "/tmp/dummy_codex_tasks",
        "CODEX_MEMORY_ROOT": "/tmp/dummy_codex_mem",
        "TELEGRAM_BOT_TOKEN": "dummy_token",
        "TELEGRAM_ALLOWED_USER_ID": "12345",
    }):
        _orig_load_settings = config.load_settings
        def _dummy_load_settings(env_file=".env"):
            isolated_env = os.devnull if env_file == ".env" else env_file
            return _orig_load_settings(env_file=isolated_env)
        with patch.object(config, "load_settings", side_effect=_dummy_load_settings):
            import bot


def _settings(root: Path) -> Settings:
    return replace(
        Settings(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=12345,
            codex_workspace_root=root / "workspace",
            codex_bin="codex",
            codex_task_root=root / "tasks",
            codex_model=None,
            codex_timeout_seconds=30,
            codex_retry_429_delays_seconds=(),
            telegram_progress_seconds=1,
            codex_memory_root=root,
            user_timezone="UTC",
            agents_enabled=True,
        ),
        chat_mode="auto",
        chat_api_key="test",
        chat_model="mock",
        lark_allowed_open_id="ou_owner",
    )


class MockPort(OutboundPort):
    supports_inline_buttons: bool = True
    supports_attachments: bool = False

    def __init__(self, supports_inline_buttons: bool = True):
        self.supports_inline_buttons = supports_inline_buttons
        self.replies: list[str] = []
        self.button_replies: list[tuple[str, list]] = []

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "msg-reply"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "msg-send-new"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.replies.append(text)
        return True

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list) -> str:
        self.button_replies.append((text, buttons))
        self.replies.append(text)
        return "msg-button-reply"


class TelegramPresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "workspace").mkdir(parents=True, exist_ok=True)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)
        self.settings = _settings(self.root)

        env_patch = patch.dict(os.environ, {
            "CODEX_WORKSPACE_ROOT": str(self.root / "workspace"),
            "CODEX_TASK_ROOT": str(self.root / "tasks"),
            "CODEX_MEMORY_ROOT": str(self.root),
            "TELEGRAM_BOT_TOKEN": "test_bot_token",
            "TELEGRAM_ALLOWED_USER_ID": "12345",
            "CONVEYOR_NAMED_AGENTS": "1",
            "CONVEYOR_AGENT_CONTEXT": "1",
            "CONVEYOR_AGENT_MAILBOX": "1",
            "CONVEYOR_AGENT_DELEGATION": "1",
            "CONVEYOR_MAIN_ROUTER": "1",
            "CONVEYOR_AGENT_CHANNEL_ROUTING": "1",
            "CONVEYOR_AGENTS_KILL": "0",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

        prior_bot_settings = bot.settings
        settings_patch = patch.object(bot, "settings", self.settings)
        settings_patch.start()
        # Supervisor hook: verify prior bot.settings identity is restored after cleanup
        self.addCleanup(lambda: self.assertIs(bot.settings, prior_bot_settings))
        self.addCleanup(settings_patch.stop)

        self.store = AgentStore(self.settings)
        self.finance = self.store.create({
            "name": "Finance",
            "persona": "financial analysis",
            "tools_allowlist": ["notes.search"],
        })
        self.source = ac.resolve(self.settings, "web", chat_id_for("default"), WEB_OPERATOR)
        self.target = ac.resolve(self.settings, "web", chat_id_for(self.finance["id"]), WEB_OPERATOR)

    def tg_msg(self, text: str = "", chat_id: str = "12345", operator_id: str = "12345", chat_type: str = "p2p") -> InboundMessage:
        return InboundMessage(
            channel="telegram",
            chat_id=chat_id,
            operator_id=operator_id,
            message_id="m1",
            text=text,
            chat_type=chat_type,
        )

    def _create_task_in_db(
        self,
        task_id: str,
        status: str,
        attempts: int = 0,
        max_attempts: int = 3,
        deadline: float | None = None,
        lease_expires_at: float | None = None,
        origin_chat: str = "12345",
        origin_operator: str = "12345",
        origin_channel: str = "telegram",
        route_agent_id: str | None = None,
        to_agent: str | None = None,
        outbox_state: str | None = None,
        created_at: float | None = None,
    ) -> None:
        db_path = Path(self.settings.codex_memory_root) / "state" / "job_queue.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        target_route_agent = route_agent_id or self.finance["id"]
        target_mailbox_agent = to_agent or self.finance["id"]
        with sqlite3.connect(str(db_path)) as conn:
            import main_router_results
            import agent_result_outbox
            conn.execute(main_router_results._SCHEMA)
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_mailbox (
                id TEXT PRIMARY KEY, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
                operator TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                deadline REAL, created_at REAL NOT NULL, lease_owner TEXT,
                lease_expires_at REAL, parent_message_id TEXT, depth INTEGER NOT NULL DEFAULT 0)""")
            agent_result_outbox.ensure(conn)

            app_key = f"delegate-token-{task_id}"
            conn.execute(
                """INSERT OR REPLACE INTO main_router_routes
                   (approval_key, agent_id, agent_name, origin_channel, origin_operator, origin_chat, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (app_key, target_route_agent, "Finance", origin_channel, origin_operator, origin_chat)
            )
            now = created_at if created_at is not None else time.time()
            dl = deadline if deadline is not None else now + 3600
            conn.execute(
                """INSERT OR REPLACE INTO agent_mailbox
                   (id, from_agent, to_agent, operator, kind, body, idempotency_key,
                    status, attempts, max_attempts, deadline, created_at, lease_expires_at)
                   VALUES (?, 'default', ?, 'web-console', 'task', '{"secret":"do_not_leak"}',
                           ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, target_mailbox_agent, app_key, status, attempts, max_attempts, dl, now, lease_expires_at)
            )
            if outbox_state is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO agent_result_notifications
                       (id, result_id, task_id, channel, operator, chat_id, text, state, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'text', ?, ?, ?)""",
                    (f"notif-{task_id}", f"res-{task_id}", task_id, origin_channel, origin_operator, origin_chat, outbox_state, now, now)
                )

    # -------------------------------------------------------------------------
    # 1. Confirmation human labels + unchanged approval payload
    # -------------------------------------------------------------------------
    async def test_telegram_route_confirmation_human_labels(self):
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Review quarterly expenses and balance sheets",
            "tools_subset": ["notes.search"],
        })
        msg = self.tg_msg()
        port = MockPort()

        await _request_confirmation(msg, port, self.settings, "main.route_delegate", payload)

        self.assertEqual(len(port.button_replies), 1)
        text, buttons = port.button_replies[0]

        # Must display human labels: 目标助手, task summary, capability summary
        self.assertIn("目标助手: Finance", text)
        self.assertIn("Review quarterly expenses", text)
        self.assertIn("notes.search", text)
        self.assertIn("确认转交？", text)

        # Must NOT show raw internal terminology as primary UX
        self.assertNotIn(self.finance["id"], text)
        self.assertNotIn("tools_subset", text)
        self.assertNotIn("风险: write", text)
        self.assertNotIn("main.route_delegate", text)

        # Buttons must have confirm and cancel
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0][0]["text"], "✅ 确认")
        self.assertEqual(buttons[0][1]["text"], "❌ 取消")

    async def test_telegram_route_confirmation_invalid_target_fails_closed(self):
        payload_missing = json.dumps({
            "to": "non-existent-agent-id",
            "task": "do something",
            "tools_subset": [],
        })
        port = MockPort()
        await _request_confirmation(self.tg_msg(), port, self.settings, "main.route_delegate", payload_missing)
        self.assertEqual(len(port.button_replies), 0)
        self.assertIn("无法转交任务", port.replies[-1])

        self.store.archive(self.finance["id"])
        payload_archived = json.dumps({
            "to": self.finance["id"],
            "task": "do something",
            "tools_subset": [],
        })
        port2 = MockPort()
        await _request_confirmation(self.tg_msg(), port2, self.settings, "main.route_delegate", payload_archived)
        self.assertEqual(len(port2.button_replies), 0)
        self.assertIn("无法转交任务", port2.replies[-1])

    async def test_telegram_route_confirmation_preserves_backend_pending_payload(self):
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Summarize balances",
            "tools_subset": ["notes.search"],
        })
        msg = self.tg_msg()
        port = MockPort()

        await _request_confirmation(msg, port, self.settings, "main.route_delegate", payload)
        _text, buttons = port.button_replies[0]
        token = buttons[0][0]["callback_data"].split("tool:confirm:")[-1]

        pending = get_pending(token)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.tool_name, "main.route_delegate")
        # Backend approval payload remains exact JSON
        self.assertEqual(pending.arg, payload)
        self.assertEqual(pending.channel, "telegram")
        self.assertEqual(pending.operator_id, "12345")
        self.assertEqual(pending.chat_id, "12345")

    # -------------------------------------------------------------------------
    # 2. Queue success vs refusal formatting
    # -------------------------------------------------------------------------
    async def test_queue_ack_success_formatting(self):
        task_id = "a1b2c3d4e5f678901234567890abcdef"
        backend_result = f"agent task queued: {task_id}"

        class FakeAction:
            tool_name = "main.route_delegate"
            arg = json.dumps({"to": self.finance["id"], "task": "Check budget", "tools_subset": []})

        ack_text, buttons = tg_pres.format_queue_ack(self.settings, self.tg_msg(), FakeAction(), backend_result)

        self.assertIn("已交给 Finance，正在排队。完成后会在这里通知你。", ack_text)
        self.assertIn("任务编号: a1b2c3d4e5f6", ack_text)
        self.assertNotIn("处理中", ack_text)
        self.assertNotIn("运行中", ack_text)
        self.assertIsNotNone(buttons)
        self.assertEqual(buttons[0][0]["text"], "🔍 查看状态")
        self.assertEqual(buttons[0][0]["callback_data"], f"task:status:{task_id}")

    async def test_queue_ack_refusal_mapping(self):
        class FakeAction:
            tool_name = "main.route_delegate"
            arg = json.dumps({"to": self.finance["id"], "task": "test", "tools_subset": []})

        refusals = {
            "main route denied": "转交失败：请求被拒绝或权限不足。",
            "main route disabled": "转交未开启：主路由功能已停用。",
            "agent delegation denied": "转交失败：代理委托被拒绝",
            "agent tools disabled by emergency kill switch": "转交阻止：紧急停止开关已激活",
            "human approval required": "转交失败：需要人工审批确认。",
            "unknown tool": "转交失败：未知工具。",
        }
        for err, expected in refusals.items():
            ack_text, buttons = tg_pres.format_queue_ack(self.settings, self.tg_msg(), FakeAction(), err)
            self.assertIn(expected, ack_text)
            self.assertIsNone(buttons)

        # Unknown failure remains generic safe reason without exposing private raw payload
        ack_text, buttons = tg_pres.format_queue_ack(self.settings, self.tg_msg(), FakeAction(), "secret_internal_error_stack")
        self.assertEqual(ack_text, "转交未执行：系统拒绝或处理异常。")
        self.assertNotIn("secret_internal_error_stack", ack_text)
        self.assertIsNone(buttons)

    async def test_backend_enqueue_contract_intact(self):
        # Backend main_router.confirm must return literal 'agent task queued: <uuid>'
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Backend contract verification",
            "tools_subset": [],
        })
        pending = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="12345",
            chat_id="12345",
            channel="telegram",
        )
        answer = router.confirm(self.settings, pending, pending.token)
        self.assertTrue(answer.startswith("agent task queued: "))

    # -------------------------------------------------------------------------
    # 3. Real execute_confirmed integration (Telegram vs Feishu vs fallback)
    # -------------------------------------------------------------------------
    async def test_execute_confirmed_telegram_ack_with_buttons(self):
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Integration test delegation",
            "tools_subset": ["notes.search"],
        })
        pending = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="12345",
            chat_id="12345",
            channel="telegram",
        )
        msg = self.tg_msg()
        port = MockPort(supports_inline_buttons=True)

        handled = await execute_confirmed(msg, port, self.settings, pending.token)
        self.assertTrue(handled)
        self.assertEqual(len(port.button_replies), 1)
        text, buttons = port.button_replies[0]
        self.assertIn("已交给 Finance，正在排队。完成后会在这里通知你。", text)
        self.assertEqual(buttons[0][0]["text"], "🔍 查看状态")
        self.assertTrue(buttons[0][0]["callback_data"].startswith("task:status:"))

    async def test_execute_confirmed_telegram_ack_fallback_without_buttons(self):
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Integration fallback delegation",
            "tools_subset": [],
        })
        pending = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="12345",
            chat_id="12345",
            channel="telegram",
        )
        msg = self.tg_msg()
        port = MockPort(supports_inline_buttons=False)

        handled = await execute_confirmed(msg, port, self.settings, pending.token)
        self.assertTrue(handled)
        self.assertEqual(len(port.button_replies), 0)
        self.assertEqual(len(port.replies), 1)
        text = port.replies[0]
        self.assertIn("已交给 Finance，正在排队。完成后会在这里通知你。", text)
        self.assertIn("/agentstatus", text)

    async def test_execute_confirmed_feishu_contract_unchanged(self):
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Feishu contract test",
            "tools_subset": [],
        })
        pending = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="ou_owner",
            chat_id="ou_owner",
            channel="feishu",
        )
        feishu_msg = InboundMessage(
            channel="feishu",
            chat_id="ou_owner",
            operator_id="ou_owner",
            message_id="feishu_m1",
            text="confirm",
            chat_type="p2p",
        )
        port = MockPort()

        handled = await execute_confirmed(feishu_msg, port, self.settings, pending.token)
        self.assertTrue(handled)
        self.assertEqual(len(port.replies), 1)
        self.assertTrue(port.replies[0].startswith("agent task queued: "))
        self.assertNotIn("已交给", port.replies[0])

    async def test_execute_confirmed_refusal_telegram_vs_feishu(self):
        # Invalid target agent
        payload = json.dumps({
            "to": "non-existent-agent",
            "task": "Invalid target test",
            "tools_subset": [],
        })
        # Telegram receives mapped Chinese error
        pending_tg = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="12345",
            chat_id="12345",
            channel="telegram",
        )
        port_tg = MockPort()
        await execute_confirmed(self.tg_msg(), port_tg, self.settings, pending_tg.token)
        self.assertIn("转交失败：请求被拒绝或权限不足。", port_tg.replies[0])

        # Feishu receives literal raw error
        pending_feishu = create_pending(
            tool_name="main.route_delegate",
            arg=payload,
            operator_id="ou_owner",
            chat_id="ou_owner",
            channel="feishu",
        )
        feishu_msg = InboundMessage(
            channel="feishu",
            chat_id="ou_owner",
            operator_id="ou_owner",
            message_id="feishu_m2",
            text="confirm",
            chat_type="p2p",
        )
        port_feishu = MockPort()
        await execute_confirmed(feishu_msg, port_feishu, self.settings, pending_feishu.token)
        self.assertEqual(port_feishu.replies[0], "main route denied")

    # -------------------------------------------------------------------------
    # 4. Status queries: lifecycle states, attempts cap, timeouts, dead
    # -------------------------------------------------------------------------
    async def test_status_queued(self):
        task_id = "00000000000000000000000000000001"
        self._create_task_in_db(task_id, status="queued")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "正在排队")
        self.assertFalse(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("状态: 正在排队", display)
        self.assertNotIn("secret", display)
        self.assertNotIn("(queued)", display)
        self.assertIsNotNone(buttons)

    async def test_status_running_leased(self):
        task_id = "00000000000000000000000000000002"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=1, lease_expires_at=now + 60)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "处理中")
        self.assertFalse(info["is_terminal"])

    async def test_status_leased_expired_with_attempts_remaining(self):
        # Lease expired, but attempts < max_attempts -> truthful 等待重试
        task_id = "00000000000000000000000000000003"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=1, max_attempts=3, lease_expires_at=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "等待重试")
        self.assertFalse(info["is_terminal"])

    async def test_status_leased_expired_with_attempts_exhausted(self):
        # Lease expired, attempts >= max_attempts -> terminal 已失败
        task_id = "00000000000000000000000000000004"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=3, max_attempts=3, lease_expires_at=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已失败")
        self.assertTrue(info["is_terminal"])

    async def test_status_leased_expired_past_deadline(self):
        # Lease expired and past deadline -> terminal 已超时
        task_id = "00000000000000000000000000000005"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=1, max_attempts=3, deadline=now - 10, lease_expires_at=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已超时")
        self.assertTrue(info["is_terminal"])

    async def test_status_failed_with_attempts_remaining(self):
        # failed state but attempts < max_attempts -> 等待重试
        task_id = "00000000000000000000000000000006"
        self._create_task_in_db(task_id, status="failed", attempts=1, max_attempts=3)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "等待重试")
        self.assertFalse(info["is_terminal"])

    async def test_status_failed_with_attempts_exhausted(self):
        # failed state and attempts >= max_attempts -> 已失败
        task_id = "00000000000000000000000000000007"
        self._create_task_in_db(task_id, status="failed", attempts=3, max_attempts=3)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已失败")
        self.assertTrue(info["is_terminal"])

    async def test_status_failed_past_deadline(self):
        # failed state and past deadline -> 已超时
        task_id = "00000000000000000000000000000008"
        now = time.time()
        self._create_task_in_db(task_id, status="failed", attempts=1, max_attempts=3, deadline=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已超时")
        self.assertTrue(info["is_terminal"])

    async def test_status_dead_generic_stopped(self):
        # dead task does not distinguish cancelled from revoked; reports generic 已停止
        task_id = "00000000000000000000000000000009"
        now = time.time()
        self._create_task_in_db(task_id, status="dead", attempts=1, deadline=now + 3600)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已停止")
        self.assertTrue(info["is_terminal"])

    async def test_status_dead_past_deadline(self):
        task_id = "00000000000000000000000000000010"
        now = time.time()
        self._create_task_in_db(task_id, status="dead", attempts=1, deadline=now - 50)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已超时")
        self.assertTrue(info["is_terminal"])

    # -------------------------------------------------------------------------
    # 5. Outbox delivery inspection & refresh button
    # -------------------------------------------------------------------------
    async def test_status_done_outbox_sent(self):
        task_id = "00000000000000000000000000000011"
        self._create_task_in_db(task_id, status="done", outbox_state="sent")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已发送。")
        self.assertTrue(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("结果已发送。", display)
        self.assertIsNone(buttons)

    async def test_status_done_outbox_pending_or_retry_allows_refresh(self):
        # When outbox is pending or retry, is_terminal=False and refresh button is shown
        for out_st in ("pending", "retry"):
            task_id = f"0000000000000000000000000000001{1 if out_st == 'pending' else 2}"
            self._create_task_in_db(task_id, status="done", outbox_state=out_st)
            info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
            self.assertIsNone(err)
            self.assertEqual(info["state_label"], "已完成")
            self.assertEqual(info["detail"], "结果已生成，正在等待发送。")
            self.assertFalse(info["is_terminal"])

            display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
            self.assertIn("结果已生成，正在等待发送。", display)
            self.assertIsNotNone(buttons)
            self.assertEqual(buttons[0][0]["text"], "🔄 刷新状态")
            self.assertEqual(buttons[0][0]["callback_data"], f"task:status:{task_id}")

    async def test_status_done_outbox_dead_no_fake_sent(self):
        task_id = "00000000000000000000000000000013"
        self._create_task_in_db(task_id, status="done", outbox_state="dead")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已生成，但通知发送未成功。")
        self.assertTrue(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("结果已生成，但通知发送未成功。", display)
        self.assertIsNone(buttons)

    async def test_status_done_no_outbox_row(self):
        task_id = "00000000000000000000000000000014"
        self._create_task_in_db(task_id, status="done", outbox_state=None)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已生成。")
        self.assertTrue(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("结果已生成。", display)
        self.assertIsNone(buttons)

    # -------------------------------------------------------------------------
    # 6. Policy gates: kill switch and archived agent
    # -------------------------------------------------------------------------
    async def test_kill_switch_fails_closed(self):
        task_id = "00000000000000000000000000000015"
        self._create_task_in_db(task_id, status="queued")

        import agent_security
        agent_security.set_kill(self.settings, enabled=True, operator="12345", source="telegram")

        # Under emergency kill switch, feature_enabled fails closed immediately
        self.assertFalse(tg_pres.feature_enabled(self.settings))
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "功能未启用或当前不可用。")

    async def test_done_task_preserved_when_archived(self):
        # A completed task preserves 'done' status even if the agent is later archived
        task_id = "00000000000000000000000000000016"
        self._create_task_in_db(task_id, status="done", outbox_state="sent")

        # Kill switch is NOT active, but agent is archived
        self.store.archive(self.finance["id"])

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已发送。")
        self.assertTrue(info["is_terminal"])

    async def test_active_task_unavailable_when_archived(self):
        task_id = "00000000000000000000000000000017"
        self._create_task_in_db(task_id, status="queued")
        self.store.archive(self.finance["id"])

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "助手不可用")
        self.assertEqual(info["detail"], "目标助手已被归档或不可用。")
        self.assertTrue(info["is_terminal"])

    # -------------------------------------------------------------------------
    # 7. Security gates: SQL prefix scoping, wildcards, mapping, cross-owner denials
    # -------------------------------------------------------------------------
    async def test_foreign_colliding_prefix_scoped_in_sql(self):
        # User A (12345) has task prefix "abc123"
        # User B (99999) has task prefix "abc123"
        task_a = "abc12300000000000000000000000001"
        task_b = "abc12300000000000000000000000002"
        self._create_task_in_db(task_a, status="queued", origin_chat="12345", origin_operator="12345")
        self._create_task_in_db(task_b, status="queued", origin_chat="99999", origin_operator="99999")

        # Querying by prefix "abc123" for User A must return task_a without collision or ambiguity!
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), "abc123")
        self.assertIsNone(err)
        self.assertEqual(info["task_id"], task_a)

    async def test_ref_wildcard_rejected(self):
        # Underscores and % are SQL wildcards; must be rejected by hex validation
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), "abc_123")
        self.assertIsNone(info)
        self.assertEqual(err, "任务编号格式无效。")

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), "abc%123")
        self.assertIsNone(info)
        self.assertEqual(err, "任务编号格式无效。")

    async def test_wrong_agent_mapping_refused(self):
        task_id = "00000000000000000000000000000018"
        # Database row where m.to_agent does not match r.agent_id
        self._create_task_in_db(
            task_id,
            status="queued",
            route_agent_id=self.finance["id"],
            to_agent="different-agent-id",
        )
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_security_gates_cross_chat_and_operator_denied(self):
        task_id = "00000000000000000000000000000019"
        self._create_task_in_db(task_id, status="queued", origin_chat="12345", origin_operator="12345")

        # Cross-chat
        msg_wrong_chat = self.tg_msg(chat_id="99999", operator_id="12345")
        info, err = tg_pres.query_delegated_task_status(self.settings, msg_wrong_chat, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

        # Cross-operator
        msg_wrong_op = self.tg_msg(chat_id="12345", operator_id="99999")
        info, err = tg_pres.query_delegated_task_status(self.settings, msg_wrong_op, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

        # Cross-channel
        msg_wrong_ch = InboundMessage(
            channel="web", chat_id="12345", operator_id="12345",
            message_id="m1", text="", chat_type="p2p"
        )
        info, err = tg_pres.query_delegated_task_status(self.settings, msg_wrong_ch, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

        # Group chat
        msg_group = self.tg_msg(chat_type="group")
        info, err = tg_pres.query_delegated_task_status(self.settings, msg_group, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_disabled_feature_fails_closed_without_db(self):
        with patch.dict(os.environ, {"CONVEYOR_MAIN_ROUTER": "0"}):
            self.assertFalse(tg_pres.feature_enabled(self.settings))
            # Query status with non-existent DB path; must fail closed without touching SQLite
            info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), "000000000000")
            self.assertIsNone(info)
            self.assertEqual(err, "功能未启用或当前不可用。")

    # -------------------------------------------------------------------------
    # 8. Markdown table conversion & result presentation
    # -------------------------------------------------------------------------
    def test_simple_table_conversion_with_units(self):
        table = (
            "财务汇总：\n"
            "| 项目 | 金额（元） |\n"
            "| :--- | ---: |\n"
            "| 住房 | 1800 |\n"
            "| 餐饮 | 240 |\n"
            "以上为明细。"
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(table)
        self.assertIn("• 项目：住房；金额（元）：1800", converted)
        self.assertIn("• 项目：餐饮；金额（元）：240", converted)
        self.assertIn("财务汇总：", converted)
        self.assertIn("以上为明细。", converted)
        self.assertNotIn("| --- |", converted)

    def test_separator_count_mismatch_untouched(self):
        mismatched_table = (
            "| Col1 | Col2 |\n"
            "| --- | --- | --- |\n"
            "| Val1 | Val2 |"
        )
        self.assertEqual(tg_pres.convert_markdown_tables_to_bullets(mismatched_table), mismatched_table)

    def test_complex_and_malformed_tables_untouched(self):
        # 1. Escaped pipes
        escaped_table = (
            "| A \\| B | C |\n"
            "| --- | --- |\n"
            "| 1 | 2 |"
        )
        self.assertEqual(tg_pres.convert_markdown_tables_to_bullets(escaped_table), escaped_table)

        # 2. Pipe inside inline code span
        code_pipe_table = (
            "| Syntax | Desc |\n"
            "| --- | --- |\n"
            "| `x | y` | Bitwise or |"
        )
        self.assertEqual(tg_pres.convert_markdown_tables_to_bullets(code_pipe_table), code_pipe_table)

        # 3. Row width mismatch (header has 2 cols, row has 3)
        unequal_table = (
            "| Col1 | Col2 |\n"
            "| --- | --- |\n"
            "| Val1 | Val2 | ExtraVal |"
        )
        self.assertEqual(tg_pres.convert_markdown_tables_to_bullets(unequal_table), unequal_table)

    def test_fenced_code_blocks_multiple_ticks_preserved(self):
        code_block = (
            "````markdown\n"
            "| Col1 | Col2 |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n"
            "````"
        )
        self.assertEqual(tg_pres.convert_markdown_tables_to_bullets(code_block), code_block)

    async def test_telegram_result_header_and_stable_tag_and_bounding(self):
        result_id = "res-uuid-12345678"
        result_tag = result_id[:12]
        body = "分析结果:\n| 项目 | 金额 |\n| --- | --- |\n| A | 100 |\n" + ("x" * 3000)
        text = tg_pres.format_telegram_result("Finance", result_tag, body)

        self.assertTrue(text.startswith("[Finance] 已完成"))
        self.assertIn(f"[result:{result_tag}]", text)
        self.assertIn("• 项目：A；金额：100", text)
        self.assertNotIn("| --- |", text)
        self.assertLessEqual(len(text), 2400)

    # -------------------------------------------------------------------------
    # 9. Bot callback and command path integration
    # -------------------------------------------------------------------------
    async def test_bot_callback_integration_anchored_and_valid(self):
        import bot

        task_id = "11111111111111111111111111111111"
        self._create_task_in_db(task_id, status="queued")

        # Mock update for valid callback
        valid_query = AsyncMock()
        valid_query.data = f"task:status:{task_id}"
        valid_query.message = AsyncMock(message_id=100)
        update_valid = AsyncMock()
        update_valid.callback_query = valid_query
        update_valid.effective_user.id = 12345
        update_valid.effective_chat.id = 12345
        update_valid.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_valid, AsyncMock())
            self.assertTrue(any("正在排队" in r for r in outbound.replies))
            self.assertEqual(len(outbound.button_replies), 1)

        # Mock update for invalid callback (unanchored or bad chars)
        invalid_query = AsyncMock()
        invalid_query.data = "task:status:not_hex_!!!"
        update_invalid = AsyncMock()
        update_invalid.callback_query = invalid_query

        with patch("bot._guard", return_value=True):
            await bot.task_status_callback(update_invalid, AsyncMock())
            invalid_query.answer.assert_called_with("无效的任务请求", show_alert=True)

    async def test_bot_agent_status_cmd_integration(self):
        import bot

        task_id = "22222222222222222222222222222222"
        self._create_task_in_db(task_id, status="queued")

        update_cmd = AsyncMock()
        update_cmd.effective_user.id = 12345
        update_cmd.effective_chat.id = 12345
        update_cmd.effective_chat.type = "private"
        context = SimpleNamespace(args=[task_id])

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_status_cmd(update_cmd, context)
            self.assertTrue(any("正在排队" in r for r in outbound.replies))

        # Command with no args displays recent tasks list with buttons
        context_empty = SimpleNamespace(args=[])
        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_status_cmd(update_cmd, context_empty)
            self.assertTrue(any(task_id[:12] in r for r in outbound.replies))
            self.assertTrue(any("最近转交任务" in r for r in outbound.replies))
            self.assertEqual(len(outbound.button_replies), 1)

        # Command with invalid hex
        context_invalid = SimpleNamespace(args=["bad_ref!"])
        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort()
            mock_outbound.return_value = outbound
            await bot.agent_status_cmd(update_cmd, context_invalid)
            self.assertIn("任务编号格式无效。", outbound.replies[0])

    async def test_bot_callback_and_command_denials(self):
        import bot

        task_id = "33333333333333333333333333333333"
        self._create_task_in_db(task_id, status="queued", origin_chat="12345", origin_operator="12345")

        # 1. Forged callback from cross-chat / cross-operator user
        query_forged = AsyncMock()
        query_forged.data = f"task:status:{task_id}"
        update_forged = AsyncMock()
        update_forged.callback_query = query_forged
        update_forged.effective_user.id = 99999
        update_forged.effective_chat.id = 99999
        update_forged.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort()
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_forged, AsyncMock())
            self.assertIn("未找到该任务或无权查看。", outbound.replies[0])

        # 2. Callback from group chat
        query_group = AsyncMock()
        query_group.data = f"task:status:{task_id}"
        update_group = AsyncMock()
        update_group.callback_query = query_group
        update_group.effective_user.id = 12345
        update_group.effective_chat.id = 12345
        update_group.effective_chat.type = "group"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort()
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_group, AsyncMock())
            self.assertIn("未找到该任务或无权查看。", outbound.replies[0])

        # 3. Flags-off callback and command without DB
        with patch.dict(os.environ, {"CONVEYOR_MAIN_ROUTER": "0"}):
            self.assertFalse(tg_pres.feature_enabled(self.settings))
            with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
                outbound = MockPort()
                mock_outbound.return_value = outbound
                valid_query = AsyncMock()
                valid_query.data = f"task:status:{task_id}"
                update_valid = AsyncMock()
                update_valid.callback_query = valid_query
                update_valid.effective_user.id = 12345
                update_valid.effective_chat.id = 12345
                update_valid.effective_chat.type = "private"

                await bot.task_status_callback(update_valid, AsyncMock())
                self.assertEqual(outbound.replies[0], "功能未启用或当前不可用。")

                outbound.replies.clear()
                context = SimpleNamespace(args=[task_id])
                await bot.agent_status_cmd(update_valid, context)
                self.assertEqual(outbound.replies[0], "功能未启用或当前不可用。")

    async def test_recent_delegated_tasks_ordering_and_deterministic_tiebreak(self):
        base_time = 1000000.0
        # 6 tasks: task 0-3 with distinct timestamps; task 4 and 5 with identical timestamp for tiebreak
        self._create_task_in_db("000000000000000000000000000000a1", status="queued", created_at=base_time + 10)
        self._create_task_in_db("000000000000000000000000000000a2", status="queued", created_at=base_time + 20)
        self._create_task_in_db("000000000000000000000000000000a3", status="queued", created_at=base_time + 30)
        self._create_task_in_db("000000000000000000000000000000a4", status="queued", created_at=base_time + 40)
        # Identical timestamp tiebreak: a6 (id higher) should beat a5
        self._create_task_in_db("000000000000000000000000000000a5", status="queued", created_at=base_time + 50)
        self._create_task_in_db("000000000000000000000000000000a6", status="queued", created_at=base_time + 50)

        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg(), limit=5)
        self.assertIsNone(err)
        self.assertEqual(len(tasks), 5)
        # Order should be newest first: a6 (50), a5 (50), a4 (40), a3 (30), a2 (20). a1 (10) omitted due to limit 5.
        returned_ids = [t["task_id"] for t in tasks]
        self.assertEqual(returned_ids, [
            "000000000000000000000000000000a6",
            "000000000000000000000000000000a5",
            "000000000000000000000000000000a4",
            "000000000000000000000000000000a3",
            "000000000000000000000000000000a2",
        ])

        display, buttons = tg_pres.get_recent_tasks_display(self.settings, self.tg_msg())
        self.assertIn("最近转交任务", display)
        self.assertIn("[Finance]", display)
        self.assertEqual(len(buttons), 6)  # 5 tasks + 1 refresh button
        # Each task button has task:status:<id>
        for i in range(5):
            self.assertEqual(buttons[i][0]["callback_data"], f"task:status:{returned_ids[i]}")
        # Bottom button refreshes recent list
        self.assertEqual(buttons[5][0]["callback_data"], "task:recent")

    async def test_recent_delegated_tasks_scoping_before_limit(self):
        base_time = 2000000.0
        # 5 scoped tasks with older timestamps
        for i in range(5):
            self._create_task_in_db(
                f"1111111111111111111111111111110{i}",
                status="queued",
                origin_chat="12345",
                origin_operator="12345",
                origin_channel="telegram",
                created_at=base_time + i,
            )

        # 5 foreign tasks with NEWER timestamps that must NOT be selected
        # 1. Foreign chat
        self._create_task_in_db(
            "99999999999999999999999999999901",
            status="queued",
            origin_chat="99999",
            origin_operator="12345",
            origin_channel="telegram",
            created_at=base_time + 100,
        )
        # 2. Foreign operator
        self._create_task_in_db(
            "99999999999999999999999999999902",
            status="queued",
            origin_chat="12345",
            origin_operator="99999",
            origin_channel="telegram",
            created_at=base_time + 101,
        )
        # 3. Foreign channel
        self._create_task_in_db(
            "99999999999999999999999999999903",
            status="queued",
            origin_chat="12345",
            origin_operator="12345",
            origin_channel="feishu",
            created_at=base_time + 102,
        )
        # 4. Route/to_agent mismatch
        self._create_task_in_db(
            "99999999999999999999999999999904",
            status="queued",
            origin_chat="12345",
            origin_operator="12345",
            origin_channel="telegram",
            route_agent_id=self.finance["id"],
            to_agent="default",  # mismatch
            created_at=base_time + 103,
        )

        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg(), limit=5)
        self.assertIsNone(err)
        self.assertEqual(len(tasks), 5)
        # Verify all returned tasks are the scoped ones
        for t in tasks:
            self.assertTrue(t["task_id"].startswith("1111111111111111111111111111110"))
            self.assertFalse(t["task_id"].startswith("9999999999999999999999999999990"))

    async def test_recent_delegated_tasks_empty_history_and_missing_tables(self):
        # Empty DB (no tasks)
        display, buttons = tg_pres.get_recent_tasks_display(self.settings, self.tg_msg())
        self.assertIn("暂无最近转交任务", display)
        self.assertIsNotNone(buttons)
        self.assertEqual(buttons[0][0]["callback_data"], "task:recent")

        # Missing DB file
        isolated_root = self.root / "empty_dir"
        isolated_root.mkdir()
        isolated_settings = _settings(isolated_root)
        tasks, err = tg_pres.query_recent_delegated_tasks(isolated_settings, self.tg_msg())
        self.assertIsNone(err)
        self.assertEqual(tasks, [])

    async def test_recent_delegated_tasks_denials(self):
        # 1. Non-owner denial
        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg(operator_id="99999"))
        self.assertIsNone(tasks)
        self.assertIn("无权查看", err)

        # 2. Group chat denial
        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg(chat_type="group"))
        self.assertIsNone(tasks)
        self.assertIn("无权查看", err)

        # 3. Channel denial
        msg_feishu = InboundMessage(channel="feishu", operator_id="12345", chat_id="12345", message_id="1", text="", chat_type="p2p")
        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, msg_feishu)
        self.assertIsNone(tasks)
        self.assertIn("无权查看", err)

        # 4. Feature flag off
        with patch.dict(os.environ, {"CONVEYOR_MAIN_ROUTER": "0"}):
            self.assertFalse(tg_pres.feature_enabled(self.settings))
            tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg())
            self.assertIsNone(tasks)
            self.assertIn("功能未启用", err)

        # 5. Kill switch
        with patch.dict(os.environ, {"CONVEYOR_AGENTS_KILL": "1"}):
            tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg())
            self.assertIsNone(tasks)
            self.assertIn("功能未启用", err)

    async def test_recent_delegated_tasks_lifecycle_and_delivery_detail(self):
        # 1. Done task with outbox 'sent' -> 结果已发送。
        self._create_task_in_db("40000000000000000000000000000001", status="done", outbox_state="sent")
        # 2. Done task with outbox 'pending' -> 结果已生成，正在等待发送。
        self._create_task_in_db("40000000000000000000000000000002", status="done", outbox_state="pending")
        # 3. Leased task with expired lease and maxed attempts -> 已失败
        self._create_task_in_db("40000000000000000000000000000003", status="leased", attempts=3, max_attempts=3, lease_expires_at=time.time() - 10)
        # 4. Leased task with expired lease and attempts remaining -> 等待重试
        self._create_task_in_db("40000000000000000000000000000004", status="leased", attempts=1, max_attempts=3, lease_expires_at=time.time() - 10)

        tasks, err = tg_pres.query_recent_delegated_tasks(self.settings, self.tg_msg(), limit=5)
        self.assertIsNone(err)
        status_map = {t["task_id"]: t for t in tasks}

        self.assertEqual(status_map["40000000000000000000000000000001"]["state_label"], "已完成")
        self.assertEqual(status_map["40000000000000000000000000000001"]["detail"], "结果已发送。")
        self.assertTrue(status_map["40000000000000000000000000000001"]["is_terminal"])

        self.assertEqual(status_map["40000000000000000000000000000002"]["state_label"], "已完成")
        self.assertEqual(status_map["40000000000000000000000000000002"]["detail"], "结果已生成，正在等待发送。")
        self.assertFalse(status_map["40000000000000000000000000000002"]["is_terminal"])

        self.assertEqual(status_map["40000000000000000000000000000003"]["state_label"], "已失败")
        self.assertTrue(status_map["40000000000000000000000000000003"]["is_terminal"])

        self.assertEqual(status_map["40000000000000000000000000000004"]["state_label"], "等待重试")
        self.assertFalse(status_map["40000000000000000000000000000004"]["is_terminal"])

        # Ensure task body/payload/secret not leaked in display
        display, _ = tg_pres.get_recent_tasks_display(self.settings, self.tg_msg())
        self.assertNotIn("secret", display)
        self.assertNotIn("do_not_leak", display)

    async def test_task_status_display_refresh_and_back_navigation(self):
        task_id = "50000000000000000000000000000001"
        self._create_task_in_db(task_id, status="queued")

        # Without include_back: PR150 behavior (only refresh button)
        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id, include_back=False)
        self.assertEqual(len(buttons[0]), 1)
        self.assertEqual(buttons[0][0]["callback_data"], f"task:status:{task_id}")

        # With include_back: includes both refresh and return to list
        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id, include_back=True)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(buttons[0][0]["callback_data"], f"task:status:{task_id}")
        self.assertEqual(buttons[0][1]["callback_data"], "task:recent")

        # Terminal task with include_back: returns only back button
        task_done_id = "50000000000000000000000000000002"
        self._create_task_in_db(task_done_id, status="done", outbox_state="sent")
        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_done_id, include_back=True)
        self.assertEqual(len(buttons[0]), 1)
        self.assertEqual(buttons[0][0]["callback_data"], "task:recent")

    async def test_bot_task_status_callback_recent_list_and_navigation(self):
        import bot
        task_id = "60000000000000000000000000000001"
        self._create_task_in_db(task_id, status="queued")

        # 1. Callback with task:recent returns recent tasks list
        query_recent = AsyncMock()
        query_recent.data = "task:recent"
        update_recent = AsyncMock()
        update_recent.callback_query = query_recent
        update_recent.effective_user.id = 12345
        update_recent.effective_chat.id = 12345
        update_recent.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_recent, AsyncMock())
            self.assertTrue(any("最近转交任务" in r for r in outbound.replies))
            self.assertTrue(any(task_id[:12] in r for r in outbound.replies))
            self.assertTrue(len(outbound.button_replies) > 0)

        # 2. Callback with valid task_id returns status detail with back button
        query_status = AsyncMock()
        query_status.data = f"task:status:{task_id}"
        update_status = AsyncMock()
        update_status.callback_query = query_status
        update_status.effective_user.id = 12345
        update_status.effective_chat.id = 12345
        update_status.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_status, AsyncMock())
            self.assertTrue(any("任务状态" in r for r in outbound.replies))
            # Verify back button is in button reply
            buttons = outbound.button_replies[-1][1]
            callbacks = [b["callback_data"] for row in buttons for b in row]
            self.assertIn("task:recent", callbacks)

    async def test_agent_chooser_display_and_selection(self):
        import agent_channel_routing as routing
        import bot

        # 1. Unbound: current assistant is Conveyor (main)
        text, buttons = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg())
        self.assertIn("当前助手: Conveyor (main)", text)
        self.assertIn("Finance", text)
        self.assertIsNotNone(buttons)

        # 2. Select Finance via bot.agent_chooser_callback
        query_sel = AsyncMock()
        query_sel.data = f"agent:select:{self.finance['id']}"
        update_sel = AsyncMock()
        update_sel.callback_query = query_sel
        update_sel.effective_user.id = 12345
        update_sel.effective_chat.id = 12345
        update_sel.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_chooser_callback(update_sel, AsyncMock())
            self.assertTrue(any("[Finance]" in r and "已切换至该助手" in r for r in outbound.replies))
            query_sel.answer.assert_called_once_with()

        # Binding persisted in DB
        self.assertEqual(routing.bound(self.settings, "telegram", "12345"), self.finance["id"])

        # 3. Chooser now shows Finance as current
        text, buttons = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg())
        self.assertIn(f"当前助手: Finance ({self.finance['id']})", text)

        # 4. Restore default via bot.agent_chooser_callback
        query_def = AsyncMock()
        query_def.data = "agent:select:default"
        update_def = AsyncMock()
        update_def.callback_query = query_def
        update_def.effective_user.id = 12345
        update_def.effective_chat.id = 12345
        update_def.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_chooser_callback(update_def, AsyncMock())
            query_def.answer.assert_called_once_with()
            self.assertTrue(any("[Conveyor]" in r and "已切换至该助手" in r for r in outbound.replies))

        self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

    async def test_agent_chooser_callback_denials_and_stale_target(self):
        import agent_channel_routing as routing
        import bot

        # 1. Non-owner denial on callback
        query_nonowner = AsyncMock()
        query_nonowner.data = f"agent:select:{self.finance['id']}"
        update_nonowner = AsyncMock()
        update_nonowner.callback_query = query_nonowner
        update_nonowner.effective_user.id = 99999
        update_nonowner.effective_chat.id = 99999
        update_nonowner.effective_chat.type = "private"

        with patch("bot._guard", return_value=True):
            await bot.agent_chooser_callback(update_nonowner, AsyncMock())
            query_nonowner.answer.assert_called_with("无权操作或功能已停用。", show_alert=True)
        self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

        # 2. Group chat denial on callback
        query_group = AsyncMock()
        query_group.data = f"agent:select:{self.finance['id']}"
        update_group = AsyncMock()
        update_group.callback_query = query_group
        update_group.effective_user.id = 12345
        update_group.effective_chat.id = 12345
        update_group.effective_chat.type = "group"

        with patch("bot._guard", return_value=True):
            await bot.agent_chooser_callback(update_group, AsyncMock())
            query_group.answer.assert_called_with("无权操作或功能已停用。", show_alert=True)

        # 3. Kill switch active
        with patch.dict(os.environ, {"CONVEYOR_AGENTS_KILL": "1"}):
            query_kill = AsyncMock()
            query_kill.data = f"agent:select:{self.finance['id']}"
            update_kill = AsyncMock()
            update_kill.callback_query = query_kill
            update_kill.effective_user.id = 12345
            update_kill.effective_chat.id = 12345
            update_kill.effective_chat.type = "private"

            with patch("bot._guard", return_value=True):
                await bot.agent_chooser_callback(update_kill, AsyncMock())
                query_kill.answer.assert_called_with("无权操作或功能已停用。", show_alert=True)

        # 4. Malformed callback data
        query_malformed = AsyncMock()
        query_malformed.data = "agent:select:invalid_chars!@#"
        update_malformed = AsyncMock()
        update_malformed.callback_query = query_malformed
        update_malformed.effective_user.id = 12345
        update_malformed.effective_chat.id = 12345
        update_malformed.effective_chat.type = "private"

        with patch("bot._guard", return_value=True):
            await bot.agent_chooser_callback(update_malformed, AsyncMock())
            query_malformed.answer.assert_called_with("无效的请求", show_alert=True)

        # 5. Stale / archived agent ID
        stale_agent = self.store.create({"name": "Stale", "persona": "soon to be archived"})
        stale_id = stale_agent["id"]
        self.store.archive(stale_id)

        query_stale = AsyncMock()
        query_stale.data = f"agent:select:{stale_id}"
        update_stale = AsyncMock()
        update_stale.callback_query = query_stale
        update_stale.effective_user.id = 12345
        update_stale.effective_chat.id = 12345
        update_stale.effective_chat.type = "private"

        with patch("bot._guard", return_value=True):
            await bot.agent_chooser_callback(update_stale, AsyncMock())
            query_stale.answer.assert_called_with("该助手已不可用或已归档。", show_alert=True)
        self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

        # 6. ID vs Name collision: an active agent named after stale_id does NOT get selected
        new_agent = self.store.create({"name": stale_id, "persona": "agent whose name is old id"})
        with patch("bot._guard", return_value=True):
            await bot.agent_chooser_callback(update_stale, AsyncMock())
            query_stale.answer.assert_called_with("该助手已不可用或已归档。", show_alert=True)
        self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

    async def test_agent_chooser_callback_bind_exception_safe(self):
        import agent_channel_routing as routing
        import bot

        query = AsyncMock()
        query.data = f"agent:select:{self.finance['id']}"
        update = AsyncMock()
        update.callback_query = query
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        update.effective_chat.type = "private"

        secret_err = "Simulated failure: /root/super/secret/path token=sk-live-secret-key-12345"
        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound, \
             patch("agent_channel_routing.bind", side_effect=RuntimeError(secret_err)):
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_chooser_callback(update, AsyncMock())

            # Answer callback preserved
            query.answer.assert_called_once_with()
            # Fixed safe Chinese message replied to chat
            self.assertTrue(any("切换助手失败，请稍后重试。" in r for r in outbound.replies))
            # No raw leak of secret or path or exception type
            self.assertFalse(any("/root/super/secret/path" in r for r in outbound.replies))
            self.assertFalse(any("sk-live-secret-key" in r for r in outbound.replies))
            self.assertFalse(any("RuntimeError" in r for r in outbound.replies))
            # Did not claim switched
            self.assertFalse(any("已切换至该助手" in r for r in outbound.replies))
            # No binding persisted
            self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

    async def test_agent_chooser_no_persona_leak(self):
        sentinel = "PRIVATE_PERSONA_SENTINEL_NEVER_SHOW_IN_TELEGRAM"
        self.store.create({
            "name": "SecretPersonaAgent",
            "persona": sentinel,
            "instructions": "SUPER_SECRET_INSTRUCTIONS_NEVER_LEAK",
            "workspace_path": "/var/secret/path",
        })

        text, buttons = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg())
        self.assertNotIn(sentinel, text)
        self.assertNotIn("SUPER_SECRET_INSTRUCTIONS", text)
        self.assertNotIn("/var/secret/path", text)
        for row in (buttons or []):
            for btn in row:
                self.assertNotIn(sentinel, btn["text"])
                self.assertNotIn(sentinel, btn["callback_data"])

    def test_format_safe_single_line_and_button_label_bounds(self):
        # 1. 60-char ASCII name
        long_name = "A" * 60
        safe_long = tg_pres.format_safe_single_line(long_name, 20)
        self.assertEqual(len(safe_long), 20)
        self.assertTrue(safe_long.startswith("AAAAAAAA"))
        self.assertTrue(safe_long.endswith("…"))
        self.assertNotIn("\n", safe_long)
        self.assertNotIn("[truncated]", safe_long)

        # 2. Multiline and control characters normalized
        multiline = "First Line\nSecond\tLine\r\nThird\x00Line"
        safe_multi = tg_pres.format_safe_single_line(multiline, 40)
        self.assertEqual(safe_multi, "First Line Second Line Third Line")
        self.assertNotIn("\n", safe_multi)
        self.assertNotIn("\t", safe_multi)
        self.assertNotIn("\x00", safe_multi)

        # 3. CJK characters bounded
        cjk_name = "这是一个非常非常非常非常非常长的助手名称用于测试截断效果"
        safe_cjk = tg_pres.format_safe_single_line(cjk_name, 15)
        self.assertEqual(len(safe_cjk), 15)
        self.assertTrue(safe_cjk.endswith("…"))

        # 4. Agent button preserves ID distinguishability and name without losing name
        agent_id = "agent_test_1234"
        id_part = f" ({agent_id})"
        avail = max(4, 32 - len(id_part))
        btn_label = f"{tg_pres.format_safe_single_line(long_name, avail)}{id_part}"
        self.assertLessEqual(len(btn_label), 32)
        self.assertTrue(btn_label.startswith("AAAA"))
        self.assertTrue(btn_label.endswith(f" ({agent_id})"))
        self.assertNotIn("\n", btn_label)
        self.assertNotIn("[truncated]", btn_label)

    async def test_agent_chooser_pagination_and_max_50_agents(self):
        import agent_channel_routing as routing
        import bot

        # Default agent exists (1). Create 48 more custom agents + self.finance = 49 custom = 50 total
        for i in range(48):
            self.store.create({
                "name": f"Agent_{i:02d}",
                "persona": f"PRIVATE_PERSONA_SENTINEL_{i}",
                "instructions": "SUPER_SECRET_INSTRUCTIONS_NEVER_LEAK",
                "workspace_path": f"/root/super/secret/path/{i}",
            })

        all_agents = [a for a in self.store.list() if not a.get("archived", False)]
        custom_agents = [a for a in all_agents if a["id"] != "default"]
        self.assertEqual(len(custom_agents), 49)

        PAGE_SIZE = 6
        expected_pages = (len(custom_agents) + PAGE_SIZE - 1) // PAGE_SIZE  # 9 pages

        seen_agent_ids_in_buttons = set()
        seen_agent_ids_in_text = set()

        # Walk all pages 1 to 9
        for p in range(1, expected_pages + 1):
            text_p, buttons_p = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg(), page=p)
            self.assertIn(f"第 {p}/{expected_pages} 页", text_p)
            self.assertNotIn("PRIVATE_PERSONA_SENTINEL", text_p)
            self.assertNotIn("SUPER_SECRET_INSTRUCTIONS", text_p)
            self.assertNotIn("/root/super/secret/path", text_p)

            # Check buttonless pagination hints in text
            if expected_pages > 1:
                self.assertIn("翻页命令：", text_p)
                if p > 1:
                    self.assertIn(f"/agents {p - 1}", text_p)
                if p < expected_pages:
                    self.assertIn(f"/agents {p + 1}", text_p)

            # Check buttons
            for row in (buttons_p or []):
                for b in row:
                    cb = b["callback_data"]
                    self.assertLess(len(cb.encode("utf-8")), 64)
                    self.assertNotIn("\n", b["text"])
                    if cb.startswith("agent:select:") and cb != "agent:select:default":
                        a_id = cb.split("agent:select:")[1]
                        seen_agent_ids_in_buttons.add(a_id)

            # Extract IDs listed in text bullets
            for line in text_p.split("\n"):
                if line.startswith("• ") and "— 默认主助手" not in line:
                    m = re.search(r"\(([a-z0-9_-]+)\)", line)
                    if m:
                        seen_agent_ids_in_text.add(m.group(1))

        # Verify all 49 custom agent IDs visited across pages with NO omissions and NO duplicates
        expected_ids = {a["id"] for a in custom_agents}
        self.assertEqual(seen_agent_ids_in_buttons, expected_ids)
        self.assertEqual(seen_agent_ids_in_text, expected_ids)

        # Off-page current binding test:
        # Bind the last agent (on page 9), then inspect page 1
        last_agent = custom_agents[-1]
        routing.bind(self.settings, self.tg_msg(), last_agent["id"], exact_id=True)
        self.assertEqual(routing.bound(self.settings, "telegram", "12345"), last_agent["id"])

        text_p1, buttons_p1 = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg(), page=1)
        self.assertIn(f"当前助手: {last_agent['name']} ({last_agent['id']})", text_p1)
        for row in buttons_p1[:-2]:
            for b in row:
                self.assertFalse(b["text"].startswith("✓ "))
        main_btn = buttons_p1[-1][0]
        self.assertFalse(main_btn["text"].startswith("✓ "))

        # Out-of-range page after archiving:
        to_archive = [a["id"] for a in custom_agents[:30]]
        for aid in to_archive:
            self.store.archive(aid)

        remaining_custom = [a for a in self.store.list() if not a.get("archived", False) and a["id"] != "default"]
        new_total_pages = (len(remaining_custom) + PAGE_SIZE - 1) // PAGE_SIZE
        text_clamped, _ = tg_pres.get_agent_chooser_display(self.settings, self.tg_msg(), page=9)
        self.assertIn(f"第 {new_total_pages}/{new_total_pages} 页", text_clamped)

    def test_feature_aware_bot_menu_commands(self):
        import bot

        baseline_cmds = [
            ("fix", "改文件：/fix <需求>"),
            ("workers", "查看 Workers 和任务状态"),
            ("sessions", "查看当前会话和历史任务"),
            ("switch", "打开会话导航（只读）"),
            ("jobs", "看最近任务"),
            ("last", "看最近结果"),
            ("diff", "看最近改动"),
            ("apply", "应用最近改动"),
            ("discard", "丢弃最近 worktree"),
            ("clean", "清理旧任务"),
            ("maintain", "自维护检查和清理"),
            ("diag", "一键诊断包"),
            ("health", "健康快照摘要"),
            ("doctor", "后端体检"),
            ("audit", "任务和 worktree 审计"),
            ("log", "安全查看最近 job 日志摘要"),
            ("meta", "查看 job.json 结构状态"),
            ("metrics", "最近任务趋势和 token 用量"),
            ("security", "安全审计"),
            ("ratelimit", "查看最近 429 限流"),
            ("smoke", "端到端验收"),
            ("editcheck", "临时 repo 真改文件验收"),
            ("memo", "显式记一条：/memo <内容>"),
            ("memory", "看今天 MEMORY.md"),
            ("journal", "看已归档的 journal 列表"),
            ("cancel", "中止当前任务"),
            ("load", "本机负载快照"),
            ("vps", "同上 (alias /load)"),
            ("htop", "top 风格进程帧"),
            ("ps", "进程快照 (comm 模式)"),
            ("tools", "列出 agent 工具"),
            ("disk", "磁盘使用快照"),
            ("logs", "Conveyor 服务日志"),
            ("service_status", "Conveyor 服务状态"),
            ("git_status", "Workspace git status"),
            ("diagnose", "Hybrid 主机诊断"),
            ("restart", "重启 Conveyor 服务"),
            ("audit_tools", "危险工具审计"),
        ]
        self.assertEqual(len(baseline_cmds), 38)
        baseline_names = [c[0] for c in baseline_cmds]

        # Mode 1. Both disabled (off-mode): exactly matches 38 baseline commands in same order
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CHANNEL_ROUTING": "0", "CONVEYOR_MAIN_ROUTER": "0"}):
            cmds = bot.get_my_bot_commands(self.settings)
            cmd_names = [c[0] for c in cmds]
            self.assertEqual(len(cmds), 38)
            self.assertEqual(cmd_names, baseline_names)
            self.assertEqual(cmds, baseline_cmds)
            self.assertNotIn("agents", cmd_names)
            self.assertNotIn("agent", cmd_names)
            self.assertNotIn("agentstatus", cmd_names)
            self.assertNotIn("status", cmd_names)

        # Mode 2. Only channel routing enabled: agents and agent at front, all 38 baseline reachable
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CHANNEL_ROUTING": "1", "CONVEYOR_MAIN_ROUTER": "0"}):
            cmds = bot.get_my_bot_commands(self.settings)
            cmd_names = [c[0] for c in cmds]
            self.assertEqual(len(cmds), 40)
            self.assertEqual(cmd_names[:2], ["agents", "agent"])
            self.assertNotIn("agentstatus", cmd_names)
            for b_name in baseline_names:
                self.assertIn(b_name, cmd_names)

        # Mode 3. Both enabled: agents, agent, agentstatus near front, all 38 baseline reachable, no duplicates
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CHANNEL_ROUTING": "1", "CONVEYOR_MAIN_ROUTER": "1"}):
            cmds = bot.get_my_bot_commands(self.settings)
            cmd_names = [c[0] for c in cmds]
            self.assertEqual(len(cmds), 41)
            self.assertEqual(cmd_names[:3], ["agents", "agent", "agentstatus"])
            self.assertEqual(len(cmd_names), len(set(cmd_names)))
            for b_name in baseline_names:
                self.assertIn(b_name, cmd_names)
            desc_map = dict(cmds)
            self.assertIn("转交任务", desc_map["agentstatus"])

    async def test_agents_cmd_and_agent_cmd_wrappers_and_routing_denials(self):
        import agent_channel_routing as routing
        import bot

        update_owner = AsyncMock()
        update_owner.effective_user.id = 12345
        update_owner.effective_chat.id = 12345
        update_owner.effective_chat.type = "private"
        context_agents = SimpleNamespace(args=[])
        context_bind = SimpleNamespace(args=["Finance"])

        # 1. Routing OFF: owner gets safe unavailable message
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CHANNEL_ROUTING": "0"}), \
             patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agents_cmd(update_owner, context_agents)
            self.assertEqual(outbound.replies, ["功能未启用或当前不可用。"])

            outbound.replies.clear()
            await bot.agent_cmd(update_owner, context_bind)
            self.assertEqual(outbound.replies, ["功能未启用或当前不可用。"])
            self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

        # 2. Kill switch active: owner gets safe unavailable message
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CHANNEL_ROUTING": "1", "CONVEYOR_AGENTS_KILL": "1"}), \
             patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agents_cmd(update_owner, context_agents)
            self.assertEqual(outbound.replies, ["功能未启用或当前不可用。"])

            outbound.replies.clear()
            await bot.agent_cmd(update_owner, context_bind)
            self.assertEqual(outbound.replies, ["功能未启用或当前不可用。"])
            self.assertIsNone(routing.bound(self.settings, "telegram", "12345"))

        # 3. Non-owner: silent refusal, no information leak
        update_nonowner = AsyncMock()
        update_nonowner.effective_user.id = 99999
        update_nonowner.effective_chat.id = 99999
        update_nonowner.effective_chat.type = "private"
        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agents_cmd(update_nonowner, context_agents)
            self.assertEqual(outbound.replies, [])
            await bot.agent_cmd(update_nonowner, context_bind)
            self.assertEqual(outbound.replies, [])

        # 4. Group chat: silent refusal
        update_group = AsyncMock()
        update_group.effective_user.id = 12345
        update_group.effective_chat.id = 12345
        update_group.effective_chat.type = "group"
        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agents_cmd(update_group, context_agents)
            self.assertEqual(outbound.replies, [])
            await bot.agent_cmd(update_group, context_bind)
            self.assertEqual(outbound.replies, [])

    def test_bot_main_handler_registration_and_callback_anchoring(self):
        import bot

        handlers_registered: list[Any] = []
        mock_app = MagicMock()
        mock_app.add_handler.side_effect = lambda h: handlers_registered.append(h)

        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.post_init.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        with patch("bot.Application.builder", return_value=mock_builder), \
             patch.object(mock_app, "run_polling"):
            bot.main()

        cmd_handlers = {h.commands[0]: h.callback for h in handlers_registered if isinstance(h, CommandHandler)}
        self.assertIn("agents", cmd_handlers)
        self.assertEqual(cmd_handlers["agents"], bot.agents_cmd)
        self.assertIn("agent", cmd_handlers)
        self.assertEqual(cmd_handlers["agent"], bot.agent_cmd)
        self.assertIn("agentstatus", cmd_handlers)
        self.assertEqual(cmd_handlers["agentstatus"], bot.agent_status_cmd)

        cb_handlers = [h for h in handlers_registered if isinstance(h, CallbackQueryHandler)]
        agent_cb = next((h for h in cb_handlers if h.pattern and h.pattern.pattern == r"^agent:"), None)
        self.assertIsNotNone(agent_cb)
        self.assertEqual(agent_cb.callback, bot.agent_chooser_callback)

        task_cb = next((h for h in cb_handlers if h.pattern and h.pattern.pattern == r"^task:"), None)
        self.assertIsNotNone(task_cb)
        self.assertEqual(task_cb.callback, bot.task_status_callback)

        # Verify pattern matching
        self.assertTrue(agent_cb.pattern.match("agent:select:Finance"))
        self.assertTrue(agent_cb.pattern.match("agent:select:default"))
        self.assertTrue(agent_cb.pattern.match("agent:page:2"))
        self.assertIsNone(agent_cb.pattern.match("task:recent"))
        self.assertIsNone(agent_cb.pattern.match("other:agent:select:1"))

        self.assertTrue(task_cb.pattern.match("task:status:0123456789abcdef"))
        self.assertTrue(task_cb.pattern.match("task:recent"))
        self.assertIsNone(task_cb.pattern.match("agent:select:default"))

    async def test_explicit_task_id_flow_preserved(self):
        import bot

        task_id = "0123456789abcdef0123456789abcdef"
        self._create_task_in_db(task_id, status="queued")

        # 1. Explicit /agentstatus <task_id> returns task status with back button
        update_cmd = AsyncMock()
        update_cmd.effective_user.id = 12345
        update_cmd.effective_chat.id = 12345
        update_cmd.effective_chat.type = "private"
        context_cmd = SimpleNamespace(args=[task_id])

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.agent_status_cmd(update_cmd, context_cmd)
            self.assertTrue(any("正在排队" in r for r in outbound.replies))
            self.assertTrue(any(task_id[:12] in r for r in outbound.replies))
            callbacks = [b["callback_data"] for row in outbound.button_replies[0] for b in row]
            self.assertIn(f"task:status:{task_id}", callbacks)
            self.assertIn("task:recent", callbacks)

        # 2. Callback task:status:<task_id> returns task status with back button
        query = AsyncMock()
        query.data = f"task:status:{task_id}"
        update_cb = AsyncMock()
        update_cb.callback_query = query
        update_cb.effective_user.id = 12345
        update_cb.effective_chat.id = 12345
        update_cb.effective_chat.type = "private"

        with patch("bot._guard", return_value=True), patch("bot.make_outbound") as mock_outbound:
            outbound = MockPort(supports_inline_buttons=True)
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_cb, AsyncMock())
            query.answer.assert_called_once_with()
            self.assertTrue(any("正在排队" in r for r in outbound.replies))
            callbacks = [b["callback_data"] for row in outbound.button_replies[0] for b in row]
            self.assertIn(f"task:status:{task_id}", callbacks)
            self.assertIn("task:recent", callbacks)

    def test_bot_settings_isolation_and_restoration_after_cleanup(self):
        """Regression test ensuring bot.settings is isolated per-test and restores prior identity."""
        # Inside this test, bot.settings has been isolated to self.settings
        self.assertIs(bot.settings, self.settings)
        self.assertEqual(bot.settings.codex_workspace_root, self.root / "workspace")

        # Simulate a nested test execution lifecycle using patch.object with addCleanup
        prior_identity = bot.settings
        dummy_temp_root = self.root / "nested_temp"
        dummy_temp_root.mkdir()
        nested_settings = _settings(dummy_temp_root)

        sub_test = unittest.TestCase()
        nested_patch = patch.object(bot, "settings", nested_settings)
        nested_patch.start()
        sub_test.addCleanup(nested_patch.stop)

        # While active, bot.settings reflects the nested settings
        self.assertIs(bot.settings, nested_settings)
        self.assertEqual(bot.settings.codex_workspace_root, dummy_temp_root / "workspace")

        # Execute cleanup on the sub_test
        sub_test.doCleanups()

        # After cleanup, prior bot.settings identity is completely restored without leaking temp roots
        self.assertIs(bot.settings, prior_identity)
        self.assertEqual(bot.settings.codex_workspace_root, self.root / "workspace")


if __name__ == "__main__":
    unittest.main()
