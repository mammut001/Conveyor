"""Tainted outbound calls must not escape automatic execution."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import egress_taint
from channel.types import InboundMessage
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.runner import _invoke_tool, run_tool
from personal_tools.registry import register_personal_tools


class Port:
    supports_inline_buttons = False

    def __init__(self):
        self.replies = []

    async def reply(self, msg, text):
        self.replies.append(text)
        return None


class TaintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="egress-taint-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = SimpleNamespace(
            codex_memory_root=self.root, egress_proxy_enabled=True,
            egress_allowlist="", approval_inbox_enabled=True,
            draft_cards_enabled=False, outbound_allowlist="",
            mcp_enabled=False, approval_relay_enabled=False)
        self.msg = InboundMessage(channel="web", operator_id="operator",
                                  chat_id="session-a", message_id="m1", text="read")
        register_personal_tools()
        clear_all_pending()
        self.addCleanup(clear_all_pending)

    def _context(self, session=None):
        return {"operator_id": self.msg.operator_id, "channel": self.msg.channel,
                "chat_id": session or self.msg.chat_id}

    def test_flag_off_no_taint_or_new_gate(self):
        self.settings.egress_proxy_enabled = False
        egress_taint.mark_read(self.settings, "memory.search", **self._context())
        self.assertFalse(egress_taint.is_tainted(self.settings, **self._context()))
        self.assertFalse(egress_taint.requires_approval(
            self.settings, "email.send", **self._context()))

    def test_private_read_marks_only_its_session_and_survives_reopen(self):
        egress_taint.mark_read(self.settings, "gmail.read", **self._context())
        self.assertTrue(egress_taint.is_tainted(self.settings, **self._context()))
        self.assertFalse(egress_taint.is_tainted(self.settings, **self._context("session-b")))
        self.assertTrue(egress_taint.requires_approval(
            self.settings, "email.send", **self._context()))
        self.assertTrue(egress_taint.requires_approval(
            self.settings, "github.create_issue", **self._context()))
        self.assertFalse(egress_taint.requires_approval(
            self.settings, "notes.list_recent", **self._context()))
        # A second DB connection sees the marker.
        self.assertTrue(egress_taint.is_tainted(self.settings, **self._context()))

    def test_gmail_read_at_run_tool_entry_marks_taint(self):
        async def fake_gmail(*args, **kwargs):
            return "private inbox response"

        with mock.patch("personal_tools.registry.execute_personal_tool",
                        new=mock.AsyncMock(side_effect=fake_gmail)):
            result = asyncio.run(run_tool(self.settings, "gmail.read", "1",
                                          **self._context()))
        self.assertEqual(result, "private inbox response")
        self.assertTrue(egress_taint.is_tainted(self.settings, **self._context()))

    def test_tainted_direct_send_is_denied_without_execution(self):
        egress_taint.mark_read(self.settings, "memory.search", **self._context())
        with mock.patch("personal_tools.registry.execute_personal_tool",
                        new_callable=mock.AsyncMock) as send:
            result = asyncio.run(run_tool(self.settings, "email.send",
                                          "to@example.com | Test | secret",
                                          **self._context()))
            self.assertIn("approval required", result)
            send.assert_not_awaited()

    def test_tainted_send_enters_existing_approval_inbox(self):
        egress_taint.mark_read(self.settings, "gmail.recent", **self._context())
        port = Port()
        with mock.patch("handlers.tools.runner.run_tool",
                        new_callable=mock.AsyncMock) as auto_send:
            asyncio.run(_invoke_tool(self.msg, port, self.settings, "email.send",
                                     "to@example.com | Test | secret"))
            auto_send.assert_not_awaited()
        pending = get_pending_for_context(**self._context())
        self.assertIsNotNone(pending)
        self.assertEqual(pending.tool_name, "email.send")
        self.assertIn("确认", port.replies[0])

    def test_without_approval_surface_a_tainted_send_fails_closed(self):
        self.settings.approval_inbox_enabled = False
        egress_taint.mark_read(self.settings, "secrets.read", **self._context())
        port = Port()
        asyncio.run(_invoke_tool(self.msg, port, self.settings, "email.send",
                                 "to@example.com | Test | secret"))
        self.assertTrue(port.replies)
        self.assertIn("Outbound blocked", port.replies[0])
        self.assertIsNone(get_pending_for_context(**self._context()))

    def test_explicit_clear_is_scoped(self):
        egress_taint.mark_read(self.settings, "email.inbox", **self._context())
        egress_taint.mark_read(self.settings, "memory.list", **self._context("session-b"))
        egress_taint.clear_session(self.settings, **self._context())
        self.assertFalse(egress_taint.is_tainted(self.settings, **self._context()))
        self.assertTrue(egress_taint.is_tainted(self.settings, **self._context("session-b")))


if __name__ == "__main__":
    unittest.main()
