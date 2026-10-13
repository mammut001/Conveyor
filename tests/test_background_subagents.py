"""Fake-LLM regression tests for opt-in durable background subagents."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from channel.types import InboundMessage
from handlers.tools.registry import DangerLevel, ToolSpec
from runner.chat_client import ChatConfig
import subagents


class Port:
    def __init__(self):
        self.sent = []

    async def send_new(self, msg, text):
        self.sent.append((msg, text))
        return "sent"


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flag = patch.dict(os.environ, {"CONVEYOR_SUBAGENTS": "1"})
        self.flag.start()
        self.settings = SimpleNamespace(
            codex_memory_root=Path(self.tmp.name),
            chat_tools_enabled=True,
            background_subagents_enabled=True,
            background_subagents_max=3,
            background_subagents_steps=2,
            background_subagents_tokens=256,
            background_subagents_timeout=3,
            chat_history_turns=3,
            approval_inbox_enabled=False,
        )
        self.msg = InboundMessage(channel="telegram", operator_id="user",
                                  chat_id="chat", message_id="msg", text="test")
        self.port = Port()
        self.cfg = ChatConfig(base_url="https://not-used.invalid", api_key="fake",
                              model="deepseek-chat", timeout=5, max_tokens=1000)
        self.read_schema = {"type": "function", "function": {
            "name": "gmail__recent", "description": "read",
            "parameters": {"type": "object", "properties": {"arg": {"type":"string"}}}}}
        self.write_schema = {"type": "function", "function": {
            "name": "test__write", "description": "write",
            "parameters": {"type": "object", "properties": {"arg": {"type":"string"}}}}}

    async def asyncTearDown(self):
        for key in list(subagents._RUNNING):
            subagents.cancel(self.settings, key)
        await asyncio.sleep(0.02)
        self.flag.stop()
        self.tmp.cleanup()

    async def wait_done(self, identifier, timeout=2):
        for _ in range(int(timeout / 0.01)):
            value = subagents.result(self.settings, identifier)
            if value and value["status"] in subagents._TERMINAL:
                await asyncio.sleep(0)
                return value
            await asyncio.sleep(0.01)
        self.fail("background task never completed")

    async def spawn(self, goal="research", subset=None, tools=None):
        return await subagents.spawn(self.settings, goal=goal, tools_subset=subset or [],
                                     msg=self.msg, port=self.port, config=self.cfg,
                                     parent_tools=tools or {})

    async def test_completion_and_short_message(self):
        with patch("subagents.complete_chat", new=AsyncMock(return_value={"content": "summary"})):
            result = await self.spawn()
            self.assertEqual(result["status"], "queued")
            record = await self.wait_done(result["id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["result_summary"], "summary")
        self.assertEqual(self.port.sent[0][0].chat_id, "chat")
        self.assertIn("summary", self.port.sent[0][1])

    async def test_global_concurrency_cap_and_release(self):
        self.settings.background_subagents_max = 1
        gate = asyncio.Event()

        async def pending(*_args, **_kwargs):
            await gate.wait()
            return {"content": "done"}

        with patch("subagents.complete_chat", side_effect=pending):
            first = await self.spawn()
            with self.assertRaisesRegex(ValueError, "concurrency cap"):
                await self.spawn()
            gate.set()
            self.assertEqual((await self.wait_done(first["id"]))["status"], "completed")
            await asyncio.sleep(0.03)
            second = await self.spawn()
            self.assertEqual((await self.wait_done(second["id"]))["status"], "completed")

    async def test_timeout(self):
        self.settings.background_subagents_timeout = 1

        async def blocked(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch("subagents.complete_chat", side_effect=blocked):
            task = await self.spawn()
            record = await self.wait_done(task["id"], timeout=2.3)
        self.assertEqual(record["status"], "timed_out")

    async def test_cancel_one(self):
        gate = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            await gate.wait()

        with patch("subagents.complete_chat", side_effect=blocked):
            task = await self.spawn()
            self.assertTrue(subagents.cancel(self.settings, task["id"]))
            self.assertEqual((await self.wait_done(task["id"]))["status"], "cancelled")

    async def test_cancel_parent_cascades_only_same_session(self):
        gate = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            await gate.wait()

        with patch("subagents.complete_chat", side_effect=blocked):
            children = [await self.spawn() for _ in range(2)]
            self.assertEqual(subagents.cancel_parent(
                self.settings, operator_id="other", channel="telegram", chat_id="chat"), 0)
            self.assertEqual(subagents.cancel_parent(
                self.settings, operator_id="user", channel="telegram", chat_id="chat"), 2)
            for child in children:
                self.assertEqual((await self.wait_done(child["id"]))["status"], "cancelled")

    async def test_depth_one_no_nested_spawn(self):
        depth = subagents._DEPTH.set(1)
        try:
            with self.assertRaisesRegex(ValueError, "cannot spawn"):
                await self.spawn()
            self.assertFalse(subagents.schemas(self.settings))
        finally:
            subagents._DEPTH.reset(depth)

    async def test_parent_tool_subset_required(self):
        with self.assertRaisesRegex(ValueError, "subset"):
            await self.spawn(subset=["not.delegated"], tools={"gmail.recent": self.read_schema})
        self.assertEqual(subagents.list_children(self.settings), [])

    async def test_private_read_taints_parent(self):
        from handlers import chat_tools
        import egress_taint
        calls = iter([
            {"content": "", "tool_calls": [{"id":"call-1", "type":"function", "function":{
                "name":"gmail__recent", "arguments":json.dumps({"arg":"inbox"})}}]},
            {"content":"read completed"}])
        spec = ToolSpec("gmail.recent", "Read messages", DangerLevel.READ, executor=None)

        async def fake_run(_settings, _tool, _arg, **kwargs):
            egress_taint.mark_read(self.settings, "gmail.recent", **kwargs)
            return "private info"

        with (patch("subagents.complete_chat", new=AsyncMock(side_effect=lambda *_a, **_k: next(calls))),
              patch.object(chat_tools, "_get_tool_spec", return_value=spec),
              patch.object(chat_tools, "is_exposed", return_value=True),
              patch("handlers.tools.runner.run_tool", side_effect=fake_run)):
            task = await self.spawn(subset=["gmail.recent"], tools={"gmail.recent": self.read_schema})
            record = await self.wait_done(task["id"])
            self.assertEqual(record["status"], "completed")
            self.assertTrue(egress_taint.is_tainted(
                self.settings, operator_id="user", channel="telegram", chat_id="chat"))

    async def test_inherits_parent_taint_and_routes_network_read_to_approval(self):
        from handlers import chat_tools
        import egress_taint
        self.settings.approval_inbox_enabled = True
        egress_taint.mark_read(self.settings, "gmail.recent",
                              operator_id="user", channel="telegram", chat_id="chat")
        self.assertTrue(egress_taint.is_tainted(
            self.settings, operator_id="user", channel="telegram", chat_id="chat"))
        spec = ToolSpec("web.fetch", "Network read", DangerLevel.READ, executor=None)
        model_calls = [{"content":"", "tool_calls":[{"id":"c", "function":{
            "name":"web__fetch", "arguments":'{"arg":"https://example.com"}'}}]}]
        schema = {"type":"function", "function":{"name":"web__fetch",
                  "parameters":{"type":"object", "properties":{"arg":{"type":"string"}}}}}
        with (patch("subagents.complete_chat", new=AsyncMock(side_effect=model_calls)),
              patch.object(chat_tools, "_get_tool_spec", return_value=spec),
              patch.object(chat_tools, "is_exposed", return_value=True),
              patch("handlers.tools.runner._request_confirmation", new_callable=AsyncMock) as confirm,
              patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as execute):
            child = await self.spawn(subset=["web.fetch"], tools={"web.fetch": schema})
            value = await self.wait_done(child["id"])
            self.assertEqual(value["status"], "approval_required")
            confirm.assert_awaited_once()
            execute.assert_not_awaited()

    async def test_write_routes_to_existing_approval_never_executes(self):
        from handlers import chat_tools
        calls = [{"content":"", "tool_calls":[{"id":"c", "function":{
            "name":"test__write", "arguments":'{"arg":"do it"}'}}]}]
        self.settings.approval_inbox_enabled = True
        spec = ToolSpec("test.write", "Write", DangerLevel.WRITE, executor=None)
        with (patch("subagents.complete_chat", new=AsyncMock(side_effect=calls)),
              patch.object(chat_tools, "_get_tool_spec", return_value=spec),
              patch.object(chat_tools, "is_exposed", return_value=True),
              patch("handlers.tools.runner._request_confirmation", new_callable=AsyncMock) as confirm,
              patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as execute):
            task = await self.spawn(subset=["test.write"], tools={"test.write": self.write_schema})
            record = await self.wait_done(task["id"])
            confirm.assert_awaited_once()
            execute.assert_not_awaited()
        self.assertEqual(record["status"], "approval_required")

    async def test_restart_marks_running_interrupted(self):
        subagents.reconcile(self.settings)
        with subagents._connect(self.settings) as db:
            db.execute("INSERT INTO children "
                       "(id,parent,operator,channel,chat_id,goal,status,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       ("orphan", "parent", "user", "telegram", "chat", "goal", "running", 1))
        subagents.reconcile(self.settings, force=True)
        self.assertEqual(subagents.status(self.settings, "orphan")["status"], "interrupted")

    async def test_flag_off_no_database_or_schemas(self):
        with patch.dict(os.environ, {"CONVEYOR_SUBAGENTS": "0"}):
            self.assertFalse(subagents.specs(self.settings))
            self.assertEqual(subagents.schemas(self.settings), [])
            with self.assertRaisesRegex(ValueError, "disabled"):
                await self.spawn()
            self.assertFalse((Path(self.tmp.name) / "subagents.db").exists())

    async def test_kill_switch_cancels_and_latches(self):
        async def blocked(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch("subagents.complete_chat", side_effect=blocked):
            child = await self.spawn()
            self.assertEqual(subagents.kill_all(self.settings), 1)
            self.assertFalse(subagents.enabled(self.settings))
            self.assertEqual((await self.wait_done(child["id"]))["status"], "cancelled")
        # An emergency latch lasts until process restart (reset only for tests).
        subagents._KILLED = False

    async def test_budget_limits_model_completion_tokens(self):
        confs = []

        async def fake_llm(conf, *_args, **_kwargs):
            confs.append(conf)
            return {"content": "done"}

        with patch("subagents.complete_chat", side_effect=fake_llm):
            child = await self.spawn()
            self.assertEqual((await self.wait_done(child["id"]))["status"], "completed")
        self.assertLessEqual(confs[0].max_tokens, 256 // 3)


if __name__ == "__main__":
    unittest.main()
