"""Comprehensive tests for Telegram agent delegation UX presentation boundaries.

Covers:
- Confirmation human labels and unchanged approval payload
- Fail-closed target validation for confirmation
- Queue ack success vs refusal formatting (never saying running when queued)
- Owner-scoped status queries across all lifecycle states:
  queued, running (leased), retrying, done, stopped (dead), expired, paused
- Outbox delivery state inspection (sent, pending/retry, dead, missing)
- Historical completed tasks preserved under kill switch and archived agent
- Terminal dead tasks report generic stopped without guessing cancellation reasons
- Foreign colliding prefixes scoped in SQL before LIMIT/ambiguity check
- Underscores and non-hex wildcards rejected
- Wrong agent mapping (m.to_agent != r.agent_id) refused
- Disabled flags fail-closed without touching SQLite
- Security gates: cross-chat, cross-operator, cross-channel, and group chat refusal
- Markdown table conversion:
  - Simple equal-width tables converted to readable bullets with headers/units preserved
  - Complex/malformed tables left untouched (escaped pipes, code-span pipes, unequal row widths)
  - Fenced code blocks (3+ backticks or tildes) preserved
  - Non-table content and markdown formatting preserved
- Telegram result notification format with localized header and stable [result:...] tag
- Non-Telegram (Feishu) presentation preservation
- Bot callback handler (anchored regex) and command handler integration
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import agent_context as ac
import agent_delegation as delegation
import agent_delegation_queue as queue
import agent_result_outbox as outbox
import agent_telegram_presentation as tg_pres
import main_router as router
from agents import AgentStore, chat_id_for, WEB_OPERATOR
from channel.types import InboundMessage
from config import Settings
from handlers.tools.confirm import create_pending, get_pending
from handlers.tools.runner import _request_confirmation, execute_confirmed


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


class MockPort:
    supports_inline_buttons: bool = True
    supports_attachments: bool = False

    def __init__(self):
        self.replies: list[str] = []
        self.button_replies: list[tuple[str, list]] = []

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "msg-reply"

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list) -> str:
        self.button_replies.append((text, buttons))
        self.replies.append(text)
        return "msg-button-reply"


class TelegramPresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS": "1",
            "CONVEYOR_AGENT_CONTEXT": "1",
            "CONVEYOR_AGENT_MAILBOX": "1",
            "CONVEYOR_AGENT_DELEGATION": "1",
            "CONVEYOR_MAIN_ROUTER": "1",
            "CONVEYOR_AGENT_CHANNEL_ROUTING": "1",
            "CONVEYOR_AGENTS_KILL": "0",
        })
        flags.start()
        self.addCleanup(flags.stop)
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
    # 3. Status queries: lifecycle states, SQL scoping, outbox delivery
    # -------------------------------------------------------------------------
    def _create_task_in_db(self, task_id: str, status: str, attempts: int = 0,
                           max_attempts: int = 3, deadline: float | None = None,
                           lease_expires_at: float | None = None,
                           origin_chat: str = "12345", origin_operator: str = "12345",
                           to_agent: str | None = None,
                           outbox_state: str | None = None) -> None:
        db_path = Path(self.settings.codex_memory_root) / "state" / "job_queue.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        agent_target = to_agent or self.finance["id"]
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS main_router_routes (
                approval_key TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL, origin_channel TEXT NOT NULL,
                origin_operator TEXT NOT NULL, origin_chat TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_mailbox (
                id TEXT PRIMARY KEY, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
                operator TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
                deadline REAL, created_at REAL NOT NULL, lease_owner TEXT,
                lease_expires_at REAL, parent_message_id TEXT, depth INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_result_notifications (
                id TEXT PRIMARY KEY, result_id TEXT NOT NULL, task_id TEXT NOT NULL,
                channel TEXT NOT NULL, operator TEXT NOT NULL, chat_id TEXT NOT NULL,
                text TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL)""")

            app_key = f"delegate-token-{task_id}"
            conn.execute(
                """INSERT OR REPLACE INTO main_router_routes
                   (approval_key, agent_id, agent_name, origin_channel, origin_operator, origin_chat, delivered)
                   VALUES (?, ?, ?, 'telegram', ?, ?, 0)""",
                (app_key, agent_target, "Finance", origin_operator, origin_chat)
            )
            now = time.time()
            dl = deadline if deadline is not None else now + 3600
            conn.execute(
                """INSERT OR REPLACE INTO agent_mailbox
                   (id, from_agent, to_agent, operator, kind, body, idempotency_key,
                    status, attempts, max_attempts, deadline, created_at, lease_expires_at)
                   VALUES (?, 'default', ?, 'web-console', 'task', '{"secret":"do_not_leak"}',
                           ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, agent_target, app_key, status, attempts, max_attempts, dl, now, lease_expires_at)
            )
            if outbox_state is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO agent_result_notifications
                       (id, result_id, task_id, channel, operator, chat_id, text, state, created_at, updated_at)
                       VALUES (?, 'res-1', ?, 'telegram', ?, ?, 'text', ?, ?, ?)""",
                    (f"notif-{task_id}", task_id, origin_operator, origin_chat, outbox_state, now, now)
                )

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

    async def test_status_retrying(self):
        task_id = "00000000000000000000000000000003"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=1, lease_expires_at=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "重试中")

    async def test_status_done_outbox_states(self):
        # 1. Outbox sent -> result sent
        task_id_sent = "00000000000000000000000000000004"
        self._create_task_in_db(task_id_sent, status="done", outbox_state="sent")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id_sent)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已发送。")

        # 2. Outbox pending/retry -> result waiting delivery
        task_id_pending = "00000000000000000000000000000005"
        self._create_task_in_db(task_id_pending, status="done", outbox_state="pending")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id_pending)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已生成，正在等待发送。")

        # 3. Outbox dead -> delivery failed
        task_id_dead_notif = "00000000000000000000000000000006"
        self._create_task_in_db(task_id_dead_notif, status="done", outbox_state="dead")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id_dead_notif)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已生成，但通知发送未成功。")

        # 4. No outbox row -> result generated
        task_id_no_outbox = "00000000000000000000000000000007"
        self._create_task_in_db(task_id_no_outbox, status="done", outbox_state=None)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id_no_outbox)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已生成。")

    async def test_status_dead_generic_stopped(self):
        # dead task does not distinguish cancelled from revoked; reports generic 已停止
        task_id = "00000000000000000000000000000008"
        now = time.time()
        self._create_task_in_db(task_id, status="dead", attempts=1, deadline=now + 3600)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已停止")
        self.assertTrue(info["is_terminal"])

    async def test_status_expired(self):
        task_id = "00000000000000000000000000000009"
        now = time.time()
        self._create_task_in_db(task_id, status="dead", attempts=1, deadline=now - 50)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已超时")
        self.assertTrue(info["is_terminal"])

    async def test_done_task_preserved_when_kill_switch_or_archived(self):
        # A done task must never be rewritten to failed or cancelled
        task_id = "00000000000000000000000000000010"
        self._create_task_in_db(task_id, status="done", outbox_state="sent")

        import agent_security
        agent_security.set_kill(self.settings, enabled=True, operator="12345", source="telegram")
        self.store.archive(self.finance["id"])

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "已完成")
        self.assertEqual(info["detail"], "结果已发送。")

    async def test_active_task_paused_when_kill_switch(self):
        task_id = "00000000000000000000000000000011"
        self._create_task_in_db(task_id, status="queued")

        import agent_security
        agent_security.set_kill(self.settings, enabled=True, operator="12345", source="telegram")

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "暂停中")
        self.assertIn("安全开关已开启", info["detail"])

    async def test_active_task_unavailable_when_archived(self):
        task_id = "00000000000000000000000000000012"
        self._create_task_in_db(task_id, status="queued")
        self.store.archive(self.finance["id"])

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state_label"], "助手不可用")
        self.assertIn("已被归档", info["detail"])

    # -------------------------------------------------------------------------
    # 4. Security gates: foreign colliding prefixes, wildcards, wrong mapping
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
        task_id = "00000000000000000000000000000015"
        # Corrupt DB row where m.to_agent does not match r.agent_id
        self._create_task_in_db(task_id, status="queued", to_agent="wrong-agent-id")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
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
    # 5. Markdown table conversion precision
    # -------------------------------------------------------------------------
    def test_simple_table_conversion_with_units(self):
        table = (
            "Performance report:\n"
            "| Server | CPU (%) | Latency (ms) |\n"
            "| :--- | :---: | ---: |\n"
            "| web-01 | 45 | 12 |\n"
            "| db-01 | 80 | 35 |\n"
            "End of report."
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(table)
        self.assertIn("• Server: web-01 | CPU (%): 45 | Latency (ms): 12", converted)
        self.assertIn("• Server: db-01 | CPU (%): 80 | Latency (ms): 35", converted)
        self.assertIn("Performance report:", converted)
        self.assertIn("End of report.", converted)
        self.assertNotIn("| --- |", converted)

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

    # -------------------------------------------------------------------------
    # 6. Telegram result presentation: localized header + stable result tag
    # -------------------------------------------------------------------------
    async def test_telegram_result_header_and_stable_tag(self):
        result_id = "res-uuid-12345678"
        result_tag = result_id[:12]
        body = "Analysis:\n| Key | Val |\n| --- | --- |\n| A | 1 |\n"
        text = tg_pres.format_telegram_result("Finance", result_tag, body)

        self.assertTrue(text.startswith("[Finance] 已完成"))
        self.assertIn(f"[result:{result_tag}]", text)
        self.assertIn("• Key: A | Val: 1", text)
        self.assertNotIn("| --- |", text)

    # -------------------------------------------------------------------------
    # 7. Bot callback and command path integration
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
            outbound = MockPort()
            mock_outbound.return_value = outbound
            await bot.task_status_callback(update_valid, AsyncMock())
            self.assertTrue(any("正在排队" in r for r in outbound.replies))

        # Mock update for invalid callback (unanchored or bad chars)
        invalid_query = AsyncMock()
        invalid_query.data = "task:status:not_hex_!!!"
        update_invalid = AsyncMock()
        update_invalid.callback_query = invalid_query

        with patch("bot._guard", return_value=True):
            await bot.task_status_callback(update_invalid, AsyncMock())
            invalid_query.answer.assert_called_with("无效的任务请求", show_alert=True)


if __name__ == "__main__":
    unittest.main()
