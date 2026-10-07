"""Telegram bindings reuse agent routing without changing delivery identity."""
import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

from agents import AgentError, AgentStore, agent_for_chat, conversation_for_chat, instructions_for_chat, workspace_for_chat
from channel.types import InboundMessage
from handlers.commands import run_command
from handlers.dispatch import dispatch
from job_lanes import lane_for_chat
from personal_tools.long_term_memory import owner_for_chat
from tests.test_agents import Case


class TelegramBindingTests(Case):
    def setUp(self):
        super().setUp()
        self.agent = self.store.create({
            "name": "Project", "workspace_path": "/srv/project", "instructions": "Use project conventions.",
        })

    def test_persistent_binding_drives_profile_workspace_memory_and_lane(self):
        self.store.bind_chat("telegram", "-10042", self.agent["id"])
        self.assertEqual(AgentStore(self.settings).bound_agent_id("telegram", "-10042"), self.agent["id"])
        self.assertEqual(instructions_for_chat(self.settings, "telegram", "-10042"),
                         ("Project", "Use project conventions."))
        self.assertEqual(str(workspace_for_chat(self.settings, "telegram", "-10042")), "/srv/project")
        self.assertEqual(owner_for_chat(self.settings, "1", "telegram", "-10042"), f"agent:{self.agent['id']}")
        self.assertEqual(lane_for_chat(replace(self.settings, agent_parallel_jobs=2), "telegram", "-10042"), self.agent["id"])
        for channel, chat in (("telegram", "42"), ("feishu", "-10042")):
            self.assertEqual(agent_for_chat(self.settings, channel, chat)["id"], "default")

    def test_switching_projects_preserves_pinned_conversations(self):
        self.store.bind_chat("telegram", "42", self.agent["id"])
        original = conversation_for_chat(self.settings, "telegram", "42")
        self.store.bind_chat("telegram", "42", self.agent["id"])
        self.store.bind_chat("telegram", "42", "default")
        self.assertNotEqual(conversation_for_chat(self.settings, "telegram", "42"), original)
        self.assertEqual(agent_for_chat(self.settings, "telegram", original)["id"], self.agent["id"])
        self.store.bind_chat("telegram", "42", self.agent["id"])
        self.assertEqual(conversation_for_chat(self.settings, "telegram", "42"), original)

    def test_unknown_archived_and_wrong_channel_are_rejected(self):
        with self.assertRaises(AgentError):
            self.store.bind_chat("telegram", "42", "missing")
        with self.assertRaises(AgentError):
            self.store.bind_chat("web", "42", self.agent["id"])
        self.store.archive(self.agent["id"])
        with self.assertRaises(AgentError):
            self.store.bind_chat("telegram", "42", self.agent["id"])
        self.assertIsNone(self.store.bound_agent_id("telegram", "42"))

    def test_active_worktree_or_job_blocks_initial_binding(self):
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("CREATE TABLE queued_jobs (channel TEXT, chat_id TEXT, state TEXT)")
            conn.execute("CREATE TABLE session_worktrees (channel TEXT, source_chat_id TEXT, state TEXT)")
            conn.execute("INSERT INTO queued_jobs VALUES ('telegram', '42', 'queued')")
            conn.execute("INSERT INTO session_worktrees VALUES ('telegram', '43', 'active')")
        for chat in ("42", "43"):
            with self.assertRaises(AgentError):
                self.store.bind_chat("telegram", chat, self.agent["id"])
            self.assertIsNone(self.store.bound_agent_id("telegram", chat))
        with sqlite3.connect(self.store.path) as conn:
            conn.execute("UPDATE queued_jobs SET state = 'done'")
            conn.execute("UPDATE session_worktrees SET state = 'closed'")
        for chat in ("42", "43"):
            self.store.bind_chat("telegram", chat, self.agent["id"])

    def test_archived_binding_fails_closed_before_dispatch(self):
        self.store.bind_chat("telegram", "42", self.agent["id"])
        self.store.archive(self.agent["id"])
        with self.assertRaises(AgentError):
            workspace_for_chat(self.settings, "telegram", "42")
        port, runner = Mock(), Mock()
        port.reply = AsyncMock()
        msg = InboundMessage("telegram", "1", "42", "5", "/run change files")
        asyncio.run(dispatch(msg, port, self.settings, runner))
        self.assertIn("归档", port.reply.call_args.args[1])
        self.assertEqual(runner.mock_calls, [])
        msg = replace(msg, text="/agent list")
        asyncio.run(dispatch(msg, port, self.settings, runner))
        self.assertIn("选择项目 Agent", port.reply.call_args.args[1])

    def test_dispatch_authorizes_before_binding(self):
        port = Mock(reply=AsyncMock())
        msg = InboundMessage("telegram", "999", "42", "5", f"/agent {self.agent['id']}")
        asyncio.run(dispatch(msg, port, self.settings, Mock()))
        self.assertEqual(port.reply.call_args.args[1], "Unauthorized.")
        self.assertIsNone(self.store.bound_agent_id("telegram", "42"))

    def test_commands_report_selection_and_respect_disabled_setting(self):
        port = Mock(reply=AsyncMock())
        msg = InboundMessage("telegram", "1", "42", "5", "/agent")
        asyncio.run(run_command("agent", msg, port, Mock(), self.settings, self.agent["id"]))
        self.assertIn("/srv/project", port.reply.call_args.args[1])
        asyncio.run(run_command("agent", msg, port, Mock(), replace(self.settings, agents_enabled=False), "default"))
        self.assertIn("未开启", port.reply.call_args.args[1])
        self.assertEqual(self.store.bound_agent_id("telegram", "42"), self.agent["id"])

    def test_agent_list_preserves_all_ids_across_message_chunks(self):
        created = [self.store.create({"name": "x" * 60, "workspace_path": "/" + "p" * 500}) for _ in range(12)]
        port = Mock(reply=AsyncMock())
        msg = InboundMessage("telegram", "1", "42", "5", "/agent list")
        for page in (1, 2, 3):
            asyncio.run(run_command("agent", msg, port, Mock(), self.settings, f"list {page}"))
        texts = [call.args[1] for call in port.reply.call_args_list]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 3000 for text in texts))
        for agent in created:
            self.assertIn(agent["id"], "\n".join(texts))


class TelegramCommandAddressTests(Case):
    def test_own_bot_suffix_is_normalized_without_stripping_other_bots(self):
        from types import SimpleNamespace as NS
        from channel.telegram import inbound_from_update
        bot = NS(username="ProjectBot", id=99)
        message = NS(text="/agent@projectbot list", message_id=1)
        update = NS(effective_user=NS(id=1), effective_chat=NS(id=-42, type="group"),
                    effective_message=message, get_bot=lambda: bot)
        inbound = inbound_from_update(update)
        self.assertEqual(inbound.text, "/agent list")
        self.assertTrue(inbound.mentioned_bot)
        message.text = "/agent@OtherBot list"
        self.assertEqual(inbound_from_update(update).text, "/agent@OtherBot list")
        self.assertFalse(inbound_from_update(update).mentioned_bot)
