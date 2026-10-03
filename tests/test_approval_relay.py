"""tests/test_approval_relay.py — Comprehensive tests for cross-channel approval relay."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import approval_inbox
import approval_relay
from channel.types import InboundMessage, OutboundPort
from handlers.tools.confirm import (
    PendingToolAction,
    clear_all_pending,
    create_pending,
    get_pending,
)
from handlers.job_queue import JobQueue
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


class MockOutbound(OutboundPort):
    supports_inline_buttons = True
    supports_attachments = False

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        self.messages.append(text)
        return "msg_1"

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        self.messages.append(text)
        return "msg_2"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.messages.append(text)
        return True

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list[list[dict]]) -> str | None:
        self.messages.append(text)
        return "msg_btn"


def make_test_settings(tmpdir: Path, *, enabled: bool = True, channels=("telegram", "feishu")):
    db_file = tmpdir / "test_relay.db"
    return SimpleNamespace(
        approval_relay_enabled=enabled,
        approval_inbox_enabled=True,
        approval_relay_db=db_file,
        approval_relay_channels=tuple(channels),
        codex_workspace_root=str(tmpdir / "workspace"),
        codex_task_root=str(tmpdir / "tasks"),
        codex_memory_root=tmpdir / "memory",
        telegram_bot_token="test_bot_token",
        telegram_allowed_user_id=12345,
        lark_app_id="cli_test",
        lark_app_secret="sec_test",
        lark_allowed_open_id="ou_test_user",
        conveyor_web_token="test-secret-web-token",
        conveyor_desktop_node_enabled=False,
    )


class TestApprovalRelayFlagOff(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=False)

    def tearDown(self):
        clear_all_pending()
        self.tmp.cleanup()

    def test_relay_disabled_is_noop(self):
        self.assertFalse(approval_relay.is_relay_enabled(self.settings))
        action = create_pending("notes.add", "hello", "op", "chat", "web")
        # publish should be no-op
        approval_relay.publish(self.settings, action, summary="test", danger="write", source="chat")
        self.assertFalse(self.settings.approval_relay_db.exists())

        # decide should return unknown
        outcome = approval_relay.decide(self.settings, action.token, True, via="web", decided_by="op")
        self.assertEqual(outcome, "unknown")

        # lists should be empty
        self.assertEqual(approval_relay.list_pending(self.settings), [])
        self.assertEqual(approval_relay.list_foreign_pending(self.settings), [])
        self.assertIsNone(approval_relay.get_relay_row(self.settings, action.token))

        # claim should be empty
        claimed = approval_relay.claim_decisions_for_instance(self.settings, "inst1", [action.token])
        self.assertEqual(claimed, [])

        # feature status in web control
        queue = JobQueue()
        queue.configure(self.settings, None, recover=False)
        control = WebControl(self.settings, None, queue)
        status = control.system_status()
        self.assertFalse(status["features"]["approval_relay"])


class TestApprovalRelayCoreStore(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True)
        approval_relay.reset_notifier_factory()

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        self.tmp.cleanup()

    def test_db_permissions_and_publish(self):
        action = create_pending("notes.add", "hello world", "op1", "chat1", "telegram")
        approval_relay.publish(self.settings, action, summary="Add a note", danger="write", source="chat")

        self.assertTrue(self.settings.approval_relay_db.exists())
        mode = stat.S_IMODE(self.settings.approval_relay_db.stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"Expected 0600 permissions, got {oct(mode)}")

        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertIsNotNone(row)
        self.assertEqual(row["token"], action.token)
        self.assertEqual(row["origin_channel"], "telegram")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["summary"], "Add a note")
        self.assertEqual(row["arg_preview"], "hello world")

    def test_decide_cas_and_claim(self):
        action = create_pending("notes.add", "test arg", "op1", "chat1", "telegram")
        approval_relay.publish(self.settings, action, summary="Test tool", danger="write", source="chat")

        # decide from web
        outcome = approval_relay.decide(self.settings, action.token, approve=True, via="web", decided_by="web_user")
        self.assertEqual(outcome, "won")

        # second decision loses
        outcome2 = approval_relay.decide(self.settings, action.token, approve=False, via="telegram", decided_by="tg_user")
        self.assertEqual(outcome2, "already_decided")

        # claim for origin process
        claimed = approval_relay.claim_decisions_for_instance(self.settings, "origin_inst", [action.token])
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["token"], action.token)
        self.assertEqual(claimed[0]["status"], "approved")

        # second claim returns empty (claimed once)
        claimed_again = approval_relay.claim_decisions_for_instance(self.settings, "origin_inst", [action.token])
        self.assertEqual(claimed_again, [])

        # mark local done
        approval_relay.mark_local(self.settings, action.token, "done", result_preview="success result")
        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertEqual(row["status"], "done")
        self.assertIn("success result", row["result_preview"])

    def test_first_wins_concurrent_race(self):
        action = create_pending("notes.add", "race item", "op1", "chat1", "web")
        approval_relay.publish(self.settings, action, summary="Race", danger="write", source="chat")

        outcomes = []
        barrier = threading.Barrier(2)

        def worker(approve_val, via_val):
            barrier.wait()
            res = approval_relay.decide(self.settings, action.token, approve=approve_val, via=via_val, decided_by=via_val)
            outcomes.append(res)

        t1 = threading.Thread(target=worker, args=(True, "web"))
        t2 = threading.Thread(target=worker, args=(False, "telegram"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertIn("won", outcomes)
        self.assertIn("already_decided", outcomes)
        self.assertEqual(len(outcomes), 2)

    def test_claim_once_concurrent_race(self):
        action = create_pending("notes.add", "claim item", "op1", "chat1", "telegram")
        approval_relay.publish(self.settings, action, summary="Claim race", danger="write", source="chat")
        approval_relay.decide(self.settings, action.token, approve=True, via="web", decided_by="web")

        claimed_results = []
        barrier = threading.Barrier(4)

        def claimer(idx):
            barrier.wait()
            res = approval_relay.claim_decisions_for_instance(self.settings, f"inst_{idx}", [action.token])
            if res:
                claimed_results.extend(res)

        threads = [threading.Thread(target=claimer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(claimed_results), 1, "Exactly one consumer must claim the row")

    def test_expiry(self):
        action = create_pending("notes.add", "expiring item", "op1", "chat1", "telegram")
        action.ttl_seconds = 0.05
        approval_relay.publish(self.settings, action, summary="Expiring", danger="write", source="chat")
        time.sleep(0.08)

        # decide after expiry returns expired
        outcome = approval_relay.decide(self.settings, action.token, True, via="web", decided_by="web")
        self.assertEqual(outcome, "expired")

        # lazy list expiry
        pending = approval_relay.list_pending(self.settings)
        self.assertEqual(len(pending), 0)


class TestApprovalRelayRedaction(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True)
        self.fake_notifier = approval_relay.FakeNotifier("telegram")
        approval_relay.set_notifier_factory(lambda ch, s: self.fake_notifier)

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        self.tmp.cleanup()

    def test_secret_never_stored_unredacted_in_db_or_notifications(self):
        secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        arg_with_secret = f"Deploy key {secret} for production"
        action = create_pending("notes.add", arg_with_secret, "op", "chat", "web")

        approval_relay.publish(self.settings, action, summary="Secret run", danger="write", source="chat")

        # Check raw DB file content
        raw_db_bytes = self.settings.approval_relay_db.read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw_db_bytes, "Raw secret must NEVER exist in DB bytes")

        # Check DB row
        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertNotIn(secret, row["arg_preview"])

        # Check notification content
        self.assertTrue(len(self.fake_notifier.sent) > 0)
        req = self.fake_notifier.sent[0]
        self.assertNotIn(secret, req["arg_preview"])

        # Check decide outcome notification
        approval_relay.decide(self.settings, action.token, True, via="telegram", decided_by=f"user-{secret}")
        self.assertTrue(len(self.fake_notifier.updated) > 0)
        upd = self.fake_notifier.updated[0]
        self.assertNotIn(secret, upd["text"])


class TestApprovalRelayNotifiersAndRateLimit(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True, channels=("telegram", "feishu"))
        self.tg_notifier = approval_relay.FakeNotifier("telegram")
        self.fs_notifier = approval_relay.FakeNotifier("feishu")

        def factory(ch, s):
            return self.tg_notifier if ch == "telegram" else self.fs_notifier

        approval_relay.set_notifier_factory(factory)
        approval_relay._rate_limiter.reset()

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        approval_relay._rate_limiter.reset()
        self.tmp.cleanup()

    def test_fanout_skips_origin_channel(self):
        # Origin is telegram -> should only notify feishu
        action_tg = create_pending("notes.add", "tg origin", "op", "chat", "telegram")
        approval_relay.publish(self.settings, action_tg, summary="test", danger="write", source="chat")
        self.assertEqual(len(self.tg_notifier.sent), 0)
        self.assertEqual(len(self.fs_notifier.sent), 1)

        # Origin is web -> should notify both telegram and feishu
        action_web = create_pending("notes.add", "web origin", "op", "chat", "web")
        approval_relay.publish(self.settings, action_web, summary="test", danger="write", source="chat")
        self.assertEqual(len(self.tg_notifier.sent), 1)
        self.assertEqual(len(self.fs_notifier.sent), 2)

    def test_notification_updates_on_decision(self):
        action = create_pending("notes.add", "test update", "op", "chat", "web")
        approval_relay.publish(self.settings, action, summary="test", danger="write", source="chat")

        approval_relay.decide(self.settings, action.token, approve=True, via="web", decided_by="admin")

        self.assertEqual(len(self.tg_notifier.updated), 1)
        self.assertIn("已批准", self.tg_notifier.updated[0]["text"])
        self.assertEqual(len(self.fs_notifier.updated), 1)
        self.assertIn("已批准", self.fs_notifier.updated[0]["text"])

    def test_rate_limiting(self):
        limiter = approval_relay.NotificationRateLimiter(max_count=3, window_seconds=60)
        self.assertTrue(limiter.allow("telegram"))
        self.assertTrue(limiter.allow("telegram"))
        self.assertTrue(limiter.allow("telegram"))
        self.assertFalse(limiter.allow("telegram"))
        # feishu is on a separate bucket
        self.assertTrue(limiter.allow("feishu"))


class TestApprovalRelayBotCallbacks(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True)
        approval_relay.reset_notifier_factory()

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        self.tmp.cleanup()

    def test_telegram_relay_callback_authorized_and_unauthorized(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_bot_token",
            "TELEGRAM_ALLOWED_USER_ID": "12345",
        }):
            import bot
        action = create_pending("notes.add", "note", "op", "chat", "web")
        approval_relay.publish(self.settings, action, summary="Note", danger="write", source="chat")

        # Fake authorized update
        query_auth = MagicMock()
        query_auth.data = f"relay:approve:{action.token}"
        query_auth.answer = AsyncMock()

        user_auth = SimpleNamespace(id=12345, username="operator")
        chat = SimpleNamespace(id=12345, type="private")
        update_auth = SimpleNamespace(
            effective_user=user_auth,
            effective_chat=chat,
            effective_message=MagicMock(),
            callback_query=query_auth,
        )

        with patch("bot.settings", self.settings):
            asyncio.run(bot.relay_callback(update_auth, MagicMock()))
            query_auth.answer.assert_called_once()
            row = approval_relay.get_relay_row(self.settings, action.token)
            self.assertEqual(row["status"], "approved")

        # Fake unauthorized update
        action2 = create_pending("notes.add", "note2", "op", "chat", "web")
        approval_relay.publish(self.settings, action2, summary="Note 2", danger="write", source="chat")

        query_unauth = MagicMock()
        query_unauth.data = f"relay:approve:{action2.token}"
        query_unauth.answer = AsyncMock()

        user_unauth = SimpleNamespace(id=99999, username="stranger")
        msg_mock = MagicMock()
        msg_mock.reply_text = AsyncMock()
        update_unauth = SimpleNamespace(
            effective_user=user_unauth,
            effective_chat=chat,
            effective_message=msg_mock,
            callback_query=query_unauth,
        )

        with patch("bot.settings", self.settings):
            asyncio.run(bot.relay_callback(update_unauth, MagicMock()))
            query_unauth.answer.assert_called_with("Unauthorized.", show_alert=True)
            row2 = approval_relay.get_relay_row(self.settings, action2.token)
            self.assertEqual(row2["status"], "pending")

    def test_feishu_relay_card_action(self):
        with patch.dict(os.environ, {
            "LARK_APP_ID": "cli_test",
            "LARK_APP_SECRET": "sec_test",
            "LARK_ALLOWED_OPEN_ID": "ou_test_user",
        }):
            import feishu_bot
        action = create_pending("notes.add", "feishu test", "op", "chat", "web")
        approval_relay.publish(self.settings, action, summary="FS Note", danger="write", source="chat")

        fake_msg = {
            "event": {
                "operator": {"open_id": "ou_test_user"},
                "action": {"value": {"action": "relay_approve", "token": action.token}},
                "context": {"open_chat_id": "oc_test_chat"},
            }
        }

        mock_port = MockOutbound()
        with patch("feishu_bot.settings", self.settings), \
             patch("feishu_bot.FeishuOutbound", return_value=mock_port):
            asyncio.run(feishu_bot._handle_card_action(fake_msg))
            row = approval_relay.get_relay_row(self.settings, action.token)
            self.assertEqual(row["status"], "approved")
            self.assertTrue(any("已批准" in m for m in mock_port.messages))


class TestApprovalRelayWebInboxHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = Path(cls.tmp.name)
        (cls.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        cls.settings = make_test_settings(cls.tmpdir, enabled=True)

        cls.loop = asyncio.new_event_loop()
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, None, recover=False)
        cls.control = WebControl(cls.settings, None, cls.queue)

        cls.server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=cls.control,
            loop=cls.loop,
            token=cls.settings.conveyor_web_token,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Start loop in background thread
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.tmp.cleanup()

    def setUp(self):
        clear_all_pending()

    def raw_request(self, method: str, path: str, body: str | None = None, authorized: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.settings.conveyor_web_token}"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = data
        return resp.status, parsed

    def test_web_inbox_lists_foreign_relay_items_and_counts(self):
        # Create a foreign action originated in telegram
        action = PendingToolAction(
            token="tg_tok_123",
            tool_name="notes.add",
            arg="foreign note",
            operator_id="tg_op",
            chat_id="tg_chat",
            channel="telegram",
            ttl_seconds=300.0,
        )
        approval_relay.publish(
            self.settings,
            action,
            summary="Foreign Note",
            danger="write",
            source="chat",
            origin_instance="foreign_instance_456",
        )

        status, data = self.raw_request("GET", "/api/approval-inbox")
        self.assertEqual(status, 200)
        items = data["items"]
        relay_items = [it for it in items if it.get("kind") == "relay"]
        self.assertEqual(len(relay_items), 1)
        it = relay_items[0]
        self.assertEqual(it["id"], "tg_tok_123")
        self.assertEqual(it["origin_channel"], "telegram")
        self.assertFalse(it["editable"])
        self.assertIsNone(it["draft"])

        counts = data["counts"]
        self.assertIn("telegram", counts)
        self.assertEqual(counts["telegram"], 1)

    def test_web_inbox_approve_relay_item(self):
        action = PendingToolAction(
            token="tg_tok_approve",
            tool_name="notes.add",
            arg="foreign note to approve",
            operator_id="tg_op",
            chat_id="tg_chat",
            channel="telegram",
            ttl_seconds=300.0,
        )
        approval_relay.publish(
            self.settings,
            action,
            summary="Foreign Note",
            danger="write",
            source="chat",
            origin_instance="foreign_inst",
        )

        # Submitting draft on relay item returns 400
        status, err = self.raw_request(
            "POST", f"/api/approval-inbox/{action.token}/approve",
            body=json.dumps({"draft": {"text": "edit"}}),
        )
        self.assertEqual(status, 400)

        # Standard approve returns 200 with status: accepted
        status, res = self.raw_request(
            "POST", f"/api/approval-inbox/{action.token}/approve",
            body="{}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["kind"], "relay")

        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["decided_via"], "web")

    def test_web_inbox_reject_relay_item(self):
        action = PendingToolAction(
            token="tg_tok_reject",
            tool_name="notes.add",
            arg="foreign note to reject",
            operator_id="tg_op",
            chat_id="tg_chat",
            channel="telegram",
            ttl_seconds=300.0,
        )
        approval_relay.publish(
            self.settings,
            action,
            summary="Foreign Note",
            danger="write",
            source="chat",
            origin_instance="foreign_inst",
        )

        status, res = self.raw_request(
            "POST", f"/api/approval-inbox/{action.token}/reject",
            body="{}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["kind"], "relay")

        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertEqual(row["status"], "rejected")


class TestApprovalRelayEndToEndHandoff(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True)
        approval_relay.reset_notifier_factory()

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        self.tmp.cleanup()

    def test_telegram_owned_executed_by_web_decision(self):
        # 1. Telegram process creates a pending action
        action = create_pending("notes.add", "buy tea", "tg_user", "tg_chat", "telegram")
        approval_relay.publish(self.settings, action, summary="Add note", danger="write", source="chat")

        port = MockOutbound()
        consumer = approval_relay.RelayConsumer(
            self.settings,
            channel="telegram",
            port_factory=lambda _cid: port,
        )

        # 2. Web console decides approve
        outcome = approval_relay.decide(self.settings, action.token, approve=True, via="web", decided_by="web_admin")
        self.assertEqual(outcome, "won")

        # 3. Telegram consumer polls and executes
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "Note saved: buy tea"
            claimed_count = asyncio.run(consumer.poll_once())

        self.assertEqual(claimed_count, 1)
        mock_tool.assert_called_once()
        self.assertTrue(len(port.messages) > 0)
        self.assertIn("✅ 已在 Web 批准", port.messages[0])

    def test_web_owned_executed_by_telegram_decision(self):
        # 1. Web chat / routine creates a web pending action
        action = create_pending("notes.add", "web item", "web_op", "web_chat", "web")
        approval_relay.publish(self.settings, action, summary="Web note", danger="write", source="chat")

        consumer = approval_relay.RelayConsumer(self.settings, channel="web")

        # 2. Telegram user taps approve
        outcome = approval_relay.decide(self.settings, action.token, approve=True, via="telegram", decided_by="tg_op")
        self.assertEqual(outcome, "won")

        # 3. Web consumer polls and executes via decide_tool_approval
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "Saved from telegram decision"
            claimed_count = asyncio.run(consumer.poll_once())

        self.assertEqual(claimed_count, 1)
        mock_tool.assert_called_once()
        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertEqual(row["status"], "done")


class TestApprovalRelayFakeBotScript(unittest.TestCase):
    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.db_path = str(self.tmpdir / "fake_bot_relay.db")

    def tearDown(self):
        clear_all_pending()
        self.tmp.cleanup()

    def test_fake_bot_script_functions(self):
        import scripts.approval_relay_fake_bot as fake_bot

        settings = fake_bot.make_settings(self.db_path)

        # Test network safety guard
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"}):
            with self.assertRaises(SystemExit):
                fake_bot.check_network_safety(allow_network=True)

        # Test press and list
        action = create_pending("notes.add", "test note", "op", "chat", "telegram")
        approval_relay.publish(settings, action, summary="Test", danger="write", source="chat")

        rows = fake_bot.list_pending_rows(settings)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token"], action.token)

        outcome = fake_bot.press(settings, action.token, "approve")
        self.assertEqual(outcome, "won")

        row = approval_relay.get_relay_row(settings, action.token)
        self.assertEqual(row["status"], "approved")



class TestApprovalRelayLocalGateAndIdempotency(unittest.TestCase):
    """Review fixes: local resolution is gated by the shared store, terminal
    marks notify exactly once, and expired decisions don't self-deadlock."""

    def setUp(self):
        clear_all_pending()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        (self.tmpdir / "memory").mkdir(parents=True, exist_ok=True)
        self.settings = make_test_settings(self.tmpdir, enabled=True, channels=("feishu",))
        self.fake = approval_relay.FakeNotifier("feishu")
        approval_relay.set_notifier_factory(lambda ch, s: self.fake if ch == "feishu" else None)

    def tearDown(self):
        clear_all_pending()
        approval_relay.reset_notifier_factory()
        self.tmp.cleanup()

    def _msg(self, action):
        return InboundMessage(
            channel=action.channel, operator_id=action.operator_id, chat_id=action.chat_id,
            message_id="m1", text="确认", chat_type="p2p",
        )

    def test_local_confirm_refused_after_remote_reject(self):
        from handlers.tools.runner import execute_confirmed
        action = create_pending("notes.add", "x", "tg_user", "tg_chat", "telegram")
        approval_relay.publish(self.settings, action, summary="s", danger="write", source="chat")
        self.assertEqual(
            approval_relay.decide(self.settings, action.token, False, via="web", decided_by="web"), "won"
        )
        port = MockOutbound()
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            asyncio.run(execute_confirmed(self._msg(action), port, self.settings, action.token))
        mock_tool.assert_not_called()
        self.assertIsNone(get_pending(action.token))
        self.assertIn("已在其他端处理", port.messages[-1])

    def test_local_confirm_wins_and_blocks_remote(self):
        from handlers.tools.runner import execute_confirmed
        action = create_pending("notes.add", "x", "tg_user", "tg_chat", "telegram")
        approval_relay.publish(self.settings, action, summary="s", danger="write", source="chat")
        port = MockOutbound()
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "ok"
            asyncio.run(execute_confirmed(self._msg(action), port, self.settings, action.token))
        mock_tool.assert_called_once()
        self.assertEqual(approval_relay.get_relay_row(self.settings, action.token)["status"], "done")
        self.assertEqual(
            approval_relay.decide(self.settings, action.token, False, via="web", decided_by="web"),
            "already_decided",
        )
        # Exactly one outcome update for the notification sent on publish.
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(len(self.fake.updated), 1)

    def test_mark_local_is_idempotent(self):
        action = create_pending("notes.add", "x", "op", "routine-1", "web")
        approval_relay.publish(self.settings, action, summary="s", danger="write", source="routine")
        self.assertTrue(approval_relay.mark_local(self.settings, action.token, "done", result_preview="r"))
        self.assertFalse(approval_relay.mark_local(self.settings, action.token, "approved", result_preview="r2"))
        row = approval_relay.get_relay_row(self.settings, action.token)
        self.assertEqual(row["status"], "done")
        self.assertEqual(len(self.fake.updated), 1)

    def test_decide_on_expired_row_does_not_block(self):
        action = create_pending("notes.add", "x", "op", "chat", "telegram")
        action.ttl_seconds = 0.01
        approval_relay.publish(self.settings, action, summary="s", danger="write", source="chat")
        time.sleep(0.05)
        t0 = time.monotonic()
        self.assertEqual(
            approval_relay.decide(self.settings, action.token, True, via="web", decided_by="web"), "expired"
        )
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(approval_relay.get_relay_row(self.settings, action.token)["status"], "expired")
        self.assertTrue(any("过期" in u["text"] for u in self.fake.updated))

    def test_fake_bot_create_consumes_remote_approval_without_side_effects(self):
        import handlers.tools.runner as tool_runner
        import scripts.approval_relay_fake_bot as fake_bot

        settings = fake_bot.make_settings(str(self.tmpdir / "fake.db"))
        original_run_tool = tool_runner.run_tool

        def presser():
            for _ in range(200):
                rows = approval_relay.list_pending(settings)
                if rows:
                    fake_bot.press(settings, rows[0]["token"], "approve", decided_by="web")
                    return
                time.sleep(0.02)

        t = threading.Thread(target=presser)
        t.start()
        try:
            result = asyncio.run(
                fake_bot.create_and_consume(settings, "notes.add", "hello", timeout=5.0, poll_interval=0.02)
            )
        finally:
            tool_runner.run_tool = original_run_tool
            t.join()
        self.assertEqual(result["status"], "done")
        self.assertTrue(any("[fake bot] executed notes.add" in m for m in result["messages"]))


if __name__ == "__main__":
    unittest.main()
