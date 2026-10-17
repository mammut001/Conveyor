"""Tests for Telegram agent delegation UX presentation boundaries.

Covers:
- Confirmation human labels and unchanged approval payload
- Fail-closed target validation for confirmation
- Queue ack success vs refusal formatting (never saying running when queued)
- Owner-scoped status queries across all lifecycle states:
  queued, running (leased), retrying, done, failed, cancelled, expired
- Security gates: cross-chat, cross-operator, cross-channel, and group chat refusal
- Archived agent and emergency kill switch revalidation
- Clean Markdown table to plain-text bullet rows conversion, preserving code blocks
- Telegram result notification format with localized header and stable [result:...] tag
- Non-Telegram (Feishu) presentation preservation
- Flag-off behavior
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
import main_router_results as router_results
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

        # Must display human agent name, task summary, capability summary
        self.assertIn("Finance", text)
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
        # Non-existent agent ID
        payload_missing = json.dumps({
            "to": "non-existent-agent-id",
            "task": "do something",
            "tools_subset": [],
        })
        port = MockPort()
        await _request_confirmation(self.tg_msg(), port, self.settings, "main.route_delegate", payload_missing)
        self.assertEqual(len(port.button_replies), 0)
        self.assertIn("无法转交任务", port.replies[-1])

        # Archived agent
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

        # Expected Chinese string plus short stable task reference
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

        # Unknown failure remains truthful error, never turned into success
        ack_text, buttons = tg_pres.format_queue_ack(self.settings, self.tg_msg(), FakeAction(), "custom backend crash")
        self.assertIn("转交未执行：custom backend crash", ack_text)
        self.assertIsNone(buttons)

    async def test_backend_enqueue_contract_intact(self):
        # Backend main_router.confirm must return literal 'agent task queued: <uuid>'
        token = "test-token-123"
        payload = json.dumps({
            "to": self.finance["id"],
            "task": "Backend contract verification",
            "tools_subset": [],
        })
        pending = create_pending("main.route_delegate", payload, "12345", "12345", "telegram", token=token)
        answer = router.confirm(self.settings, pending, token)
        self.assertTrue(answer.startswith("agent task queued: "))

    # -------------------------------------------------------------------------
    # 3. Status queries: queued, running (leased), retrying, done, failed, cancelled, expired
    # -------------------------------------------------------------------------
    def _create_task_in_db(self, task_id: str, status: str, attempts: int = 0,
                           max_attempts: int = 3, deadline: float | None = None,
                           lease_expires_at: float | None = None) -> None:
        db_path = Path(self.settings.codex_memory_root) / "state" / "job_queue.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
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

            app_key = f"delegate-token-{task_id}"
            conn.execute(
                """INSERT OR REPLACE INTO main_router_routes
                   (approval_key, agent_id, agent_name, origin_channel, origin_operator, origin_chat, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (app_key, self.finance["id"], "Finance", "telegram", "12345", "12345")
            )
            now = time.time()
            dl = deadline if deadline is not None else now + 3600
            conn.execute(
                """INSERT OR REPLACE INTO agent_mailbox
                   (id, from_agent, to_agent, operator, kind, body, idempotency_key,
                    status, attempts, max_attempts, deadline, created_at, lease_expires_at)
                   VALUES (?, 'default', ?, 'web-console', 'task', '{"secret":"do_not_leak"}',
                           ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, self.finance["id"], app_key, status, attempts, max_attempts, dl, now, lease_expires_at)
            )

    async def test_status_queued(self):
        task_id = "00000000000000000000000000000001"
        self._create_task_in_db(task_id, status="queued")
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "queued")
        self.assertIn("正在排队", info["state_label"])
        self.assertFalse(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("正在排队 (queued)", display)
        self.assertNotIn("secret", display)
        self.assertIsNotNone(buttons)

    async def test_status_running_leased(self):
        task_id = "00000000000000000000000000000002"
        now = time.time()
        self._create_task_in_db(task_id, status="leased", attempts=1, lease_expires_at=now + 60)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "running")
        self.assertIn("处理中", info["state_label"])
        self.assertFalse(info["is_terminal"])

    async def test_status_retrying_from_expired_lease(self):
        task_id = "00000000000000000000000000000003"
        now = time.time()
        # Lease expired, attempts 1 < 3
        self._create_task_in_db(task_id, status="leased", attempts=1, lease_expires_at=now - 10)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "retrying")
        self.assertIn("等待重试", info["state_label"])
        self.assertFalse(info["is_terminal"])

    async def test_status_retrying_from_failed_turn(self):
        task_id = "00000000000000000000000000000004"
        self._create_task_in_db(task_id, status="failed", attempts=1, max_attempts=3)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "retrying")
        self.assertIn("等待重试", info["state_label"])

    async def test_status_done(self):
        task_id = "00000000000000000000000000000005"
        self._create_task_in_db(task_id, status="done", attempts=1)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "done")
        self.assertIn("已完成", info["state_label"])
        self.assertTrue(info["is_terminal"])

        display, buttons = tg_pres.get_task_status_display(self.settings, self.tg_msg(), task_id)
        self.assertIn("已完成 (done)", display)
        self.assertIn("结果已在此会话中发送", display)
        self.assertIsNone(buttons)

    async def test_status_failed_exhausted(self):
        task_id = "00000000000000000000000000000006"
        self._create_task_in_db(task_id, status="dead", attempts=3, max_attempts=3)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "failed")
        self.assertIn("已失败", info["state_label"])
        self.assertTrue(info["is_terminal"])

    async def test_status_expired_deadline(self):
        task_id = "00000000000000000000000000000007"
        now = time.time()
        self._create_task_in_db(task_id, status="dead", attempts=1, deadline=now - 50)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "expired")
        self.assertIn("已过期", info["state_label"])
        self.assertTrue(info["is_terminal"])

    async def test_status_cancelled_operator_stop(self):
        task_id = "00000000000000000000000000000008"
        now = time.time()
        # status dead, attempts < max_attempts, deadline in future
        self._create_task_in_db(task_id, status="dead", attempts=1, max_attempts=3, deadline=now + 3600)
        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertEqual(info["state"], "cancelled")
        self.assertIn("已取消", info["state_label"])
        self.assertTrue(info["is_terminal"])

    # -------------------------------------------------------------------------
    # 4. Security & Revalidation: cross-chat/operator/forged callback refusal
    # -------------------------------------------------------------------------
    async def test_status_cross_chat_refusal(self):
        task_id = "00000000000000000000000000000009"
        self._create_task_in_db(task_id, status="queued")

        # Different chat_id
        forged_msg = self.tg_msg(chat_id="999999", operator_id="12345")
        info, err = tg_pres.query_delegated_task_status(self.settings, forged_msg, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_status_cross_operator_refusal(self):
        task_id = "00000000000000000000000000000010"
        self._create_task_in_db(task_id, status="queued")

        # Different operator_id
        forged_msg = self.tg_msg(chat_id="12345", operator_id="attacker")
        info, err = tg_pres.query_delegated_task_status(self.settings, forged_msg, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_status_cross_channel_refusal(self):
        task_id = "00000000000000000000000000000011"
        self._create_task_in_db(task_id, status="queued")

        # Feishu msg querying telegram task
        feishu_msg = InboundMessage(
            channel="feishu", chat_id="12345", operator_id="ou_owner",
            message_id="m", text="", chat_type="p2p"
        )
        info, err = tg_pres.query_delegated_task_status(self.settings, feishu_msg, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_status_group_chat_refusal(self):
        task_id = "00000000000000000000000000000012"
        self._create_task_in_db(task_id, status="queued")

        group_msg = self.tg_msg(chat_type="group")
        info, err = tg_pres.query_delegated_task_status(self.settings, group_msg, task_id)
        self.assertIsNone(info)
        self.assertEqual(err, "未找到该任务或无权查看。")

    async def test_status_archived_agent_revalidation(self):
        task_id = "00000000000000000000000000000013"
        self._create_task_in_db(task_id, status="queued")
        self.store.archive(self.finance["id"])

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertIn("已归档", info["state_label"])
        self.assertTrue(info["is_terminal"])

    async def test_status_kill_switch_revalidation(self):
        task_id = "00000000000000000000000000000014"
        self._create_task_in_db(task_id, status="queued")

        import agent_security
        agent_security.set_kill(self.settings, enabled=True, operator="12345", source="telegram")

        info, err = tg_pres.query_delegated_task_status(self.settings, self.tg_msg(), task_id)
        self.assertIsNone(err)
        self.assertIn("kill switch", info["state_label"])
        self.assertTrue(info["is_terminal"])

    # -------------------------------------------------------------------------
    # 5. Markdown table to bullet rows conversion
    # -------------------------------------------------------------------------
    def test_markdown_table_2_col(self):
        table = (
            "Summary:\n"
            "| Metric | Value |\n"
            "| --- | --- |\n"
            "| Revenue | $10,000 |\n"
            "| Cost | $4,000 |\n"
            "End of report."
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(table)
        self.assertIn("• Revenue: $10,000", converted)
        self.assertIn("• Cost: $4,000", converted)
        self.assertIn("Summary:", converted)
        self.assertIn("End of report.", converted)
        self.assertNotIn("| --- |", converted)

    def test_markdown_table_3_col(self):
        table = (
            "| Server | CPU | Memory |\n"
            "| :--- | :---: | ---: |\n"
            "| web-01 | 45% | 8GB |\n"
            "| db-01 | 80% | 32GB |"
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(table)
        self.assertIn("• web-01 — CPU: 45%, Memory: 8GB", converted)
        self.assertIn("• db-01 — CPU: 80%, Memory: 32GB", converted)

    def test_markdown_table_code_blocks_preserved(self):
        code_block = (
            "Here is code:\n"
            "```python\n"
            "# | Header1 | Header2 |\n"
            "# | --- | --- |\n"
            "def test():\n"
            "    return x | y\n"
            "```\n"
            "Done."
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(code_block)
        self.assertEqual(converted, code_block)

    def test_markdown_table_non_table_preserved_and_no_html(self):
        text = (
            "## Section 1\n"
            "Regular text with **bold** and `code` and pipe A | B.\n"
            "1. List item 1\n"
            "2. List item 2\n"
        )
        converted = tg_pres.convert_markdown_tables_to_bullets(text)
        self.assertEqual(converted, text)
        self.assertNotIn("<table>", converted)
        self.assertNotIn("<br>", converted)

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
        self.assertIn("• A: 1", text)
        self.assertNotIn("| --- |", text)

    async def test_outbox_enqueue_committed_telegram_vs_feishu(self):
        db_path = Path(self.settings.codex_memory_root) / "state" / "job_queue.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        outbox.ensure(conn)

        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS main_router_routes (
                approval_key TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                agent_name TEXT NOT NULL, origin_channel TEXT NOT NULL,
                origin_operator TEXT NOT NULL, origin_chat TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0)""")

            # Telegram route
            conn.execute(
                """INSERT OR REPLACE INTO main_router_routes
                   (approval_key, agent_id, agent_name, origin_channel, origin_operator, origin_chat, delivered)
                   VALUES ('delegate-token-tg', ?, 'Finance', 'telegram', '12345', '12345', 0)""",
                (self.finance["id"],)
            )
            # Feishu route
            conn.execute(
                """INSERT OR REPLACE INTO main_router_routes
                   (approval_key, agent_id, agent_name, origin_channel, origin_operator, origin_chat, delivered)
                   VALUES ('delegate-token-fs', ?, 'Finance', 'feishu', 'ou_owner', 'oc_chat', 0)""",
                (self.finance["id"],)
            )

        task_tg = {"id": "task-tg-1", "idempotency_key": "delegate-token-tg", "to_agent": self.finance["id"]}
        task_fs = {"id": "task-fs-1", "idempotency_key": "delegate-token-fs", "to_agent": self.finance["id"]}

        # Enqueue Telegram
        self.assertTrue(outbox.enqueue_committed(conn, self.settings, task_tg, "result-tg-uuid-9999", "| Item | Val |\n| --- | --- |\n| Apple | 1 |"))
        # Enqueue Feishu
        self.assertTrue(outbox.enqueue_committed(conn, self.settings, task_fs, "result-fs-uuid-8888", "Feishu body text"))

        rows = conn.execute("SELECT * FROM agent_result_notifications ORDER BY channel").fetchall()
        self.assertEqual(len(rows), 2)

        # Feishu preserves legacy English header
        feishu_row = next(r for r in rows if r["channel"] == "feishu")
        self.assertIn("[Finance] delegated result", feishu_row["text"])
        self.assertIn("[result:result-fs-uu]", feishu_row["text"])

        # Telegram uses localized header and table bullet rows
        tg_row = next(r for r in rows if r["channel"] == "telegram")
        self.assertIn("[Finance] 已完成", tg_row["text"])
        self.assertIn("[result:result-tg-uu]", tg_row["text"])
        self.assertIn("• Apple: 1", tg_row["text"])

        conn.close()

    # -------------------------------------------------------------------------
    # 7. Flag-off legacy behavior
    # -------------------------------------------------------------------------
    async def test_flag_off_main_router(self):
        with patch.dict(os.environ, {"CONVEYOR_MAIN_ROUTER": "0"}):
            self.assertFalse(router.enabled(self.settings))
            port = MockPort()
            handled = await router.maybe_route(self.tg_msg("@Finance review"), port, self.settings)
            self.assertFalse(handled)
            self.assertEqual(port.replies, [])


if __name__ == "__main__":
    unittest.main()
