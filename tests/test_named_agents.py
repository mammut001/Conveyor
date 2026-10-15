"""P3-2 opt-in per-agent tool, memory, routine and transcript regressions."""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from channel.types import InboundMessage
from handlers.tools.confirm import create_pending, get_pending
from handlers.tools.runner import execute_confirmed
import egress_taint

from agents import AgentStore, chat_id_for, session_id_for, instructions_for_chat
from handlers.chat_tools import build_tool_schemas
from handlers.tools.runner import run_tool
from personal_tools.long_term_memory import owner_for_chat, memory_remember, memory_search
from personal_tools.routines import routine_list, routine_pause
from transcript_store import get_transcript_store
import named_agents
import routines


def settings_for(root):
    from config import Settings
    return replace(Settings(
        telegram_bot_token="test-token",
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
    ), long_term_memory_enabled=True, routines_enabled=True)


def _migrate_old_agent_db(path, start, queue):
    try:
        if not start.wait(10):
            raise RuntimeError("migration start timed out")
        named_agents.ensure_schema(path)
        queue.put(("ok", ""))
    except BaseException as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))


class MigrationRaceTests(unittest.TestCase):
    def test_four_processes_migrate_old_schema_without_duplicate_columns(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("POSIX fork required by Linux migration regression")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for iteration in range(4):
                db = root / f"old-{iteration}.sqlite3"
                with sqlite3.connect(db) as conn:
                    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT)")
                    conn.execute("INSERT INTO agents(id, name) VALUES('default', 'Conveyor')")
                ctx = multiprocessing.get_context("fork")
                start, queue = ctx.Event(), ctx.Queue()
                children = [ctx.Process(target=_migrate_old_agent_db,
                                        args=(str(db), start, queue)) for _ in range(4)]
                try:
                    for child in children:
                        child.start()
                    start.set()
                    results = [queue.get(timeout=15) for _ in children]
                    for child in children:
                        child.join(timeout=10)
                    self.assertTrue(all(x == ("ok", "") for x in results), results)
                    self.assertTrue(all(child.exitcode == 0 for child in children))
                    with sqlite3.connect(db) as conn:
                        columns = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
                        self.assertTrue({"persona", "memory_scope", "tools_allowlist"} <= columns)
                        default = conn.execute("SELECT memory_scope FROM agents WHERE id='default'").fetchone()
                        self.assertEqual(default[0], "operator")
                finally:
                    for child in children:
                        if child.is_alive():
                            child.terminate()
                            child.join(timeout=5)
                    queue.close()


class NamedAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"CONVEYOR_NAMED_AGENTS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = settings_for(Path(self.temp.name))
        self.store = AgentStore(self.settings)
        self.a = self.store.create({"name": "Alpha", "persona": "Research analyst.",
                                    "tools_allowlist": ["notes.search", "agent.message",
                                                        "memory.remember", "memory.search",
                                                        "routine.list", "routine.pause"]})
        self.b = self.store.create({"name": "Beta", "persona": "Quality reviewer.",
                                    "tools_allowlist": ["notes.search", "memory.search",
                                                        "routine.list"]})
        self.a_chat = chat_id_for(self.a["id"])
        self.b_chat = chat_id_for(self.b["id"])

    async def test_schema_is_scoped_and_dispatch_is_fail_closed(self):
        exposed = lambda chat: {s["function"]["name"] for s in build_tool_schemas(
            self.settings, channel="web", chat_id=chat, operator_id="web-console")}
        alpha, beta = exposed(self.a_chat), exposed(self.b_chat)
        self.assertIn("agent__message", alpha)
        self.assertNotIn("agent__message", beta)
        self.assertIn("notes__search", alpha & beta)
        self.assertNotIn("notes__add", alpha | beta)
        self.assertFalse(named_agents.tool_allowed(
            self.settings, "web", self.b_chat, "agent.message"))
        self.assertEqual(await run_tool(self.settings, "notes.add", "secret",
                                        channel="web", chat_id=self.a_chat), "agent tool denied")
        self.assertEqual(await run_tool(self.settings, "agent.message", "{}",
                                        channel="web", chat_id=self.b_chat), "agent tool denied")
        self.store.archive(self.b["id"])
        self.assertNotIn("notes__search", exposed(self.b_chat))

    async def test_default_agent_legacy_tool_inventory_flag_on_off(self):
        for channel, chat_id in (("telegram", "12345"),
                                 ("feishu", "ou_12345"),
                                 ("web", "web-console"),
                                 ("web", "legacy-chat")):
            with self.subTest(channel=channel, chat_id=chat_id):
                identity = "web-console" if channel == "web" else "owner"
                with patch.dict(os.environ, {"CONVEYOR_NAMED_AGENTS": "0"}):
                    baseline = build_tool_schemas(
                        self.settings, channel=channel, chat_id=chat_id,
                        operator_id=identity)
                active = build_tool_schemas(
                    self.settings, channel=channel, chat_id=chat_id,
                    operator_id=identity)
                self.assertEqual(active, baseline)
                self.assertGreater(len(active), 0)
                self.assertTrue(named_agents.tool_allowed(
                    self.settings, channel, chat_id, "notes.search",
                    operator_id=identity))
                result = await run_tool(
                    self.settings, "notes.search", "no matching note",
                    operator_id=identity, channel=channel, chat_id=chat_id)
                self.assertNotEqual(result, "agent tool denied")
        self.assertFalse(named_agents.tool_allowed(
            self.settings, "telegram", "12345", "agent.message"))

    async def test_execute_confirmed_rejects_revoked_allowlist(self):
        class Port:
            def __init__(self):
                self.replies = []

            async def reply(self, _msg, text):
                self.replies.append(text)
                return "ok"

        msg = InboundMessage(
            channel="web", chat_id=self.a_chat,
            operator_id="web-console", message_id="confirm-test", text="yes")
        # An approval must not bypass a new or changed tool restriction.
        pending = create_pending("notes.add", "should never run",
                                 "web-console", self.a_chat, "web")
        port = Port()
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as call:
            self.assertTrue(await execute_confirmed(
                msg, port, self.settings, pending.token))
            call.assert_not_awaited()
        self.assertIsNone(get_pending(pending.token))
        self.assertIn("agent tool denied", port.replies)

    async def test_agent_message_respects_private_egress_taint(self):
        import json
        arg = json.dumps({"to": self.b["id"], "text": "Sensitive facts should not leak"})
        with patch("egress_taint.enabled", return_value=True):
            egress_taint.mark_read(
                self.settings, "notes.search", operator_id="web-console",
                channel="web", chat_id=self.a_chat)
            self.assertTrue(egress_taint.requires_approval(
                self.settings, "agent.message", operator_id="web-console",
                channel="web", chat_id=self.a_chat))
            denied = await run_tool(
                self.settings, "agent.message", arg, operator_id="web-console",
                channel="web", chat_id=self.a_chat)
            self.assertIn("Outbound blocked", denied)
            self.assertEqual(get_transcript_store(self.settings).messages(
                session_id_for(self.b["id"])), [])
            accepted = await run_tool(
                self.settings, "agent.message", arg, operator_id="web-console",
                channel="web", chat_id=self.a_chat, egress_approved=True)
            self.assertEqual(accepted, "agent message recorded")

    async def test_memory_and_routines_are_agent_isolated(self):
        from personal_tools.long_term_memory import memory_search
        own_a = owner_for_chat(self.settings, "web-console", "web", self.a_chat)
        own_b = owner_for_chat(self.settings, "web-console", "web", self.b_chat)
        self.assertNotEqual(own_a, own_b)
        self.assertEqual(self.a["memory_scope"], "private")
        await memory_remember(self.settings, "The marmalade planet is lavender",
                              operator_id="web-console", channel="web", chat_id=self.a_chat)
        other = await memory_search(self.settings, "marmalade planet",
                                    operator_id="web-console", channel="web", chat_id=self.b_chat)
        self.assertNotIn("lavender", other.text)
        own = await memory_search(self.settings, "marmalade planet",
                                  operator_id="web-console", channel="web", chat_id=self.a_chat)
        self.assertIn("lavender", own.text)

        ra = routines.create_routine(self.settings, "alpha check", "0 8 * * *",
                                     "Check service A", origin_channel="web",
                                     origin_chat_id=self.a_chat)
        rb = routines.create_routine(self.settings, "beta check", "0 9 * * *",
                                     "Check service B", origin_channel="web",
                                     origin_chat_id=self.b_chat)
        a_list = await routine_list(self.settings, operator_id="web-console",
                                    channel="web", chat_id=self.a_chat)
        b_list = await routine_list(self.settings, operator_id="web-console",
                                    channel="web", chat_id=self.b_chat)
        self.assertIn("alpha check", a_list.text)
        self.assertNotIn("beta check", a_list.text)
        self.assertIn("beta check", b_list.text)
        denied = await routine_pause(self.settings, str(rb["id"]),
                                     operator_id="web-console", channel="web",
                                     chat_id=self.a_chat)
        self.assertFalse(denied.ok)
        self.assertTrue(routines.get_routine(self.settings, rb["id"])["enabled"])

    async def test_messages_are_recorded_in_unified_transcript(self):
        import json
        msg = json.dumps({"to": self.b["id"], "text": "Review the isolation fix."})
        response = named_agents.send_message(self.settings, msg,
                                             channel="web", chat_id=self.a_chat,
                                             operator_id="web-console")
        self.assertEqual(response, "agent message recorded")
        store = get_transcript_store(self.settings)
        incoming = store.messages(session_id_for(self.b["id"]))
        outgoing = store.messages(session_id_for(self.a["id"]))
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["kind"], "agent_message")
        self.assertEqual(incoming[0]["role"], "tool")
        self.assertIn("Review the isolation fix.", incoming[0]["content"])
        self.assertEqual(outgoing[0]["kind"], "agent_message")
        self.assertEqual(incoming[0]["metadata"]["from_agent"], self.a["id"])

    async def test_validation_and_default_off_compatibility(self):
        with self.assertRaises(ValueError):
            self.store.update(self.a["id"], {"memory_scope": "operator"})
        with self.assertRaises(ValueError):
            self.store.update(self.a["id"], {"tools_allowlist": ["*"]})
        self.assertEqual(instructions_for_chat(
            self.settings, "web", self.a_chat)[1], "Research analyst.")
        with patch.dict(os.environ, {"CONVEYOR_NAMED_AGENTS": "0"}):
            self.assertTrue(named_agents.tool_allowed(
                self.settings, "web", self.a_chat, "notes.add"))
            self.assertEqual(named_agents.send_message(self.settings, "{}",
                                                       channel="web", chat_id=self.a_chat),
                             "unknown tool")
            self.assertNotIn("agent__message", {s["function"]["name"] for s in
                build_tool_schemas(self.settings, channel="web", chat_id=self.a_chat)})
            self.assertNotIn("tools_allowlist", AgentStore(self.settings).get(self.a["id"]))
