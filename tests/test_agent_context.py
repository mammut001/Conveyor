"""P3-3 canonical identity, capability and memory isolation regressions."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_context as ac
from agents import AgentStore, chat_id_for
from channel.types import InboundMessage
from config import Settings
from handlers.chat import chat_key
from handlers.chat_tools import build_tool_schemas
from handlers.tools.runner import run_tool
from personal_tools.long_term_memory import owner_for_chat, memory_remember, memory_search


def settings_for(root):
    return replace(Settings(
        telegram_bot_token="unit-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        agents_enabled=True,
    ), long_term_memory_enabled=True)


class AgentContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CONVEYOR_NAMED_AGENTS": "1",
                                      "CONVEYOR_AGENT_CONTEXT": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.settings = settings_for(Path(self.tmp.name))
        store = AgentStore(self.settings)
        self.a = store.create({"name": "Writer", "tools_allowlist": [
            "notes.search", "memory.remember", "memory.search", "agent.message"]})
        self.b = store.create({"name": "Reviewer", "tools_allowlist": [
            "notes.search", "memory.search"]})
        self.store = store
        self.a_chat = chat_id_for(self.a["id"])
        self.b_chat = chat_id_for(self.b["id"])

    def ctx(self, chat):
        return ac.resolve(self.settings, "web", chat, "web-console")

    def msg(self, chat):
        return InboundMessage(channel="web", operator_id="web-console",
                              chat_id=chat, message_id="test", text="hello")

    def test_canonical_fields_and_isolated_history(self):
        a = self.ctx(self.a_chat)
        b = self.ctx(self.b_chat)
        self.assertEqual(a.agent_id, self.a["id"])
        self.assertEqual(a.session_id, f"web:web-console:{self.a_chat}")
        self.assertEqual(a.memory_owner, f"agent:{self.a['id']}")
        self.assertEqual(a.operator_identity, "web-console")
        self.assertEqual(a.origin_channel, "web")
        self.assertEqual(a.tool_capabilities, frozenset(self.a["tools_allowlist"]))
        self.assertIsNone(a.delegation_id)
        self.assertFalse(a.trusted_owner_request)
        self.assertNotEqual(a.memory_owner, b.memory_owner)
        self.assertNotEqual(chat_key(self.msg(self.a_chat), self.settings),
                            chat_key(self.msg(self.b_chat), self.settings))
        self.assertTrue(chat_key(self.msg(self.a_chat), self.settings).startswith(
            f"agent:{self.a['id']}:web:web-console:"))

    def test_fail_closed_missing_invalid_or_archived_identity(self):
        from named_agents import tool_allowed
        for channel, chat_id, operator_id in (
            ("", self.a_chat, "web-console"),
            ("web", "", "web-console"),
            ("web", self.a_chat, ""),
            ("web", self.a_chat, "other-operator"),
            ("web", "agent-doesnotexist", "web-console"),
        ):
            with self.subTest(channel=channel, chat_id=chat_id, operator=operator_id):
                with self.assertRaises(ac.AgentContextDenied):
                    ac.resolve(self.settings, channel, chat_id, operator_id)
                self.assertFalse(ac.allowed(self.settings, "notes.search",
                    channel=channel, chat_id=chat_id, operator_id=operator_id))
        self.assertFalse(tool_allowed(self.settings, "", "", "notes.search"))
        self.assertEqual(build_tool_schemas(self.settings), [])
        self.store.archive(self.a["id"])
        with self.assertRaises(ac.AgentContextDenied):
            self.ctx(self.a_chat)
        self.assertFalse(ac.allowed(self.settings, "notes.search", channel="web",
            chat_id=self.a_chat, operator_id="web-console"))

    def test_capability_intersection_never_uses_receiver_extra_tools(self):
        a = self.ctx(self.a_chat)
        b = self.ctx(self.b_chat)
        delegated = ac.delegate(self.settings, a, b,
            frozenset({"notes.search", "memory.remember", "memory.search",
                       "agent.message", "arbitrary.tool"}), "task_123")
        self.assertEqual(delegated.tool_capabilities,
                         frozenset({"notes.search", "memory.search"}))
        self.assertFalse(delegated.trusted_owner_request)
        self.assertEqual(delegated.delegation_id, "task_123")
        self.assertTrue(ac.allowed(self.settings, "notes.search", channel="web",
            chat_id=self.b_chat, operator_id="web-console", context=delegated))
        self.assertFalse(ac.allowed(self.settings, "memory.remember", channel="web",
            chat_id=self.b_chat, operator_id="web-console", context=delegated))
        self.assertFalse(ac.allowed(self.settings, "agent.message", channel="web",
            chat_id=self.b_chat, operator_id="web-console", context=delegated))
        with ac.bind(delegated):
            self.assertFalse(ac.allowed(self.settings, "memory.remember", channel="web",
                chat_id=self.b_chat, operator_id="web-console"))
            self.assertFalse(ac.allowed(self.settings, "notes.search", channel="web",
                chat_id=self.a_chat, operator_id="web-console"))
        forged = replace(a, memory_owner=b.memory_owner)
        with self.assertRaises(ac.AgentContextDenied):
            ac.delegate(self.settings, forged, b, {"notes.search"}, "task_bad")

    async def test_tool_dispatch_requires_identity_and_bound_capabilities(self):
        self.assertEqual(await run_tool(self.settings, "notes.search", "test"),
                         "agent tool denied")
        self.assertEqual(await run_tool(self.settings, "notes.search", "test",
            operator_id="web-console", channel="web", chat_id="agent-bad"),
            "agent tool denied")
        self.assertEqual(await run_tool(self.settings, "memory.remember", "secret",
            operator_id="web-console", channel="web", chat_id=self.b_chat),
            "agent tool denied")
        with ac.bind(ac.delegate(self.settings, self.ctx(self.a_chat),
                                 self.ctx(self.b_chat), {"notes.search"}, "job123")):
            self.assertEqual(await run_tool(self.settings, "memory.search", "anything",
                operator_id="web-console", channel="web", chat_id=self.b_chat),
                "agent tool denied")

    async def test_memory_owner_is_mandatory_even_for_memory_v2_bridge(self):
        a = self.ctx(self.a_chat)
        b = self.ctx(self.b_chat)
        self.assertEqual(owner_for_chat(self.settings, "web-console", "web", self.a_chat),
                         a.memory_owner)
        with self.assertRaises(ac.AgentContextDenied):
            owner_for_chat(self.settings, "", "", "")
        with self.assertRaises(ac.AgentContextDenied):
            await memory_remember(self.settings, "unscoped", operator_id="")
        await memory_remember(self.settings, "Aurora planet is turquoise",
            operator_id="web-console", channel="web", chat_id=self.a_chat)
        found = await memory_search(self.settings, "Aurora planet",
            operator_id="web-console", channel="web", chat_id=self.a_chat)
        other = await memory_search(self.settings, "Aurora planet",
            operator_id="web-console", channel="web", chat_id=self.b_chat)
        self.assertIn("turquoise", found.text)
        self.assertNotIn("turquoise", other.text)
        with ac.bind(a):
            with self.assertRaises(ac.AgentContextDenied):
                await memory_search(self.settings, "Aurora planet",
                    operator_id="web-console", channel="web", chat_id=self.b_chat)
        self.assertEqual(owner_for_chat(self.settings, "web-console",
                                        "web", self.b_chat), b.memory_owner)

    def test_default_off_retains_legacy_history_and_contextless_tools(self):
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CONTEXT": "0"}):
            msg = self.msg(self.a_chat)
            self.assertEqual(chat_key(msg, self.settings), f"web:{self.a_chat}")
            self.assertTrue(__import__("named_agents").tool_allowed(
                self.settings, "", "", "notes.search"))


if __name__ == "__main__":
    unittest.main()
