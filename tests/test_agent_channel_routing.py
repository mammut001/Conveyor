"""PR7: owner-only channel Agent binding, isolation, Chinese routing, outbox."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import agent_channel_routing as routing
import agent_channel_results as outbox
import agent_context as ac
import main_router
from agents import AgentStore, session_id_for
from channel.types import InboundMessage
from config import Settings
from transcript_store import get_transcript_store


def settings_for(root):
    return replace(Settings(
        telegram_bot_token="test", telegram_allowed_user_id=1,
        codex_workspace_root=root/"workspace",codex_bin="codex",
        codex_task_root=root/"tasks",codex_model=None,
        codex_timeout_seconds=30,codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,codex_memory_root=root,
        user_timezone="UTC",agents_enabled=True),
        chat_mode="auto",chat_api_key="test",chat_model="mock",
        lark_allowed_open_id="ou_owner")


class Port:
    supports_inline_buttons = False
    supports_attachments = False
    def __init__(self): self.replies = []
    async def reply(self, msg, text):
        self.replies.append((msg.chat_id,text))
        return "ok"
    async def send_new(self, msg, text):
        return await self.reply(msg,text)
    async def edit_progress(self,msg,placeholder,text):
        await self.reply(msg,text)
        return True


class ChannelRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = settings_for(Path(tmp.name))
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS":"1",
            "CONVEYOR_AGENT_CONTEXT":"1",
            "CONVEYOR_AGENT_MAILBOX":"1",
            "CONVEYOR_AGENT_DELEGATION":"1",
            "CONVEYOR_MAIN_ROUTER":"1",
            "CONVEYOR_AGENT_CHANNEL_ROUTING":"1"})
        flags.start(); self.addCleanup(flags.stop)
        self.store=AgentStore(self.settings)
        self.finance=self.store.create({"name":"Finance", "persona":"理财 投资 预算",
                                        "tools_allowlist":["notes.search"]})
        self.helper=self.store.create({"name":"Helper", "persona":"你是一个友善的助手，帮用户回答日常问题",
                                       "tools_allowlist":[]})

    def msg(self, text, channel="telegram", chat_type="p2p", operator=None, chat=None):
        return InboundMessage(
            channel=channel, operator_id=operator or
              ("1" if channel=="telegram" else "ou_owner"),
            chat_id=chat or ("1" if channel=="telegram" else "oc_private"),
            message_id="123",text=text,chat_type=chat_type)

    async def command(self,msg):
        port=Port()
        from handlers.dispatch import dispatch
        with patch("job_lanes.runner_for_chat",side_effect=lambda runner,*a:runner), \\
             patch("handlers.dispatch.try_resolve_confirmation",
                   new_callable=AsyncMock,return_value=False):
            await dispatch(msg,port,self.settings,object())
        return port

    async def test_flag_off_does_not_bind_or_route(self):
        with patch.dict(os.environ,{"CONVEYOR_AGENT_CHANNEL_ROUTING":"0"}):
            self.assertFalse(routing.enabled(self.settings))
            self.assertIsNone(routing.bound(self.settings,"telegram","1"))
            self.assertFalse(await routing.maybe_command(self.settings,self.msg("/agents"),Port()))
            self.assertEqual(ac.resolve(self.settings,"telegram","1","1").agent_id,"default")

    async def test_telegram_and_feishu_bind_persist_and_main_restore(self):
        for channel in ("telegram","feishu"):
            with self.subTest(channel=channel):
                msg=self.msg("/agent Finance",channel)
                p=await self.command(msg)
                self.assertIn("[Finance]",p.replies[-1][1])
                self.assertEqual(routing.bound(self.settings,channel,msg.chat_id),self.finance["id"])
                ctx=ac.resolve(self.settings,channel,msg.chat_id,msg.operator_id)
                self.assertEqual(ctx.agent_id,self.finance["id"])
                self.assertEqual(ctx.memory_owner,"agent:"+self.finance["id"])
                self.assertEqual(ac.history_key(self.settings,channel,msg.chat_id,msg.operator_id),
                                 "agent:"+self.finance["id"]+":"+session_id_for(self.finance["id"]))
                # Session mirror populates the existing per-Agent Web Console.
                from handlers.session import append_turn
                with ac.bind(ctx):
                    append_turn(self.settings,msg,"Investment question","Safe answer")
                assert get_transcript_store(self.settings).last_message(
                    session_id_for(self.finance["id"]))["content"]=="Safe answer"
                # Reopen SQLite storage: chat binding survives an in-process switch.
                self.assertEqual(routing.bound(self.settings,channel,msg.chat_id),self.finance["id"])
                p=await self.command(self.msg("/agent main",channel))
                self.assertIn("[Main]",p.replies[-1][1])
                self.assertIsNone(routing.bound(self.settings,channel,msg.chat_id))
                self.assertEqual(ac.resolve(self.settings,channel,msg.chat_id,msg.operator_id).agent_id,
                                 "default")

    async def test_unknown_archived_nonowner_and_group_denied(self):
        self.assertIn("Unknown", (await self.command(self.msg("/agent absent"))).replies[-1][1])
        self.store.archive(self.finance["id"])
        self.assertIn("Unknown",(await self.command(self.msg("/agent Finance"))).replies[-1][1])
        with self.assertRaises(PermissionError):
            routing.bind(self.settings,self.msg("/agent Helper",operator="2"),"Helper")
        self.assertFalse(await routing.maybe_command(
            self.settings,self.msg("/agents",chat_type="group"),Port()))
        with self.assertRaises(PermissionError):
            routing.bind(self.settings,self.msg("/agent Helper",chat_type="group"),"Helper")
        self.assertIsNone(routing.bound(self.settings,"telegram","1"))
        with patch.dict(os.environ,{"CONVEYOR_AGENT_CHANNEL_ROUTING":"0"}):
            self.assertIsNone(routing.bound(self.settings,"telegram","1"))

    async def test_list_owner_and_prefix_preserves_original_transport(self):
        port=await self.command(self.msg("/agents"))
        self.assertIn("Finance",port.replies[-1][1])
        await self.command(self.msg("/agent Helper"))
        port=Port()
        labeled=routing.label_port(self.settings,self.msg("Question"),port)
        await labeled.send_new(self.msg("Question"),"hello")
        self.assertEqual(port.replies[-1],("1","[Helper] hello"))
        self.assertEqual(ac.resolve(self.settings,"telegram","1","1").tool_capabilities,
                         frozenset())

    async def test_cjk_specialist_routes_and_generic_chat_stays_main(self):
        web_msg=InboundMessage(channel="web",operator_id="web-console",
                               chat_id="agent-default",message_id="m",
                               text="",chat_type="p2p")
        async def no_model(prompt):
            self.fail("deterministic router must not call model")
        for text in ("你好 今天可以帮我一下吗", "请问今天是什么日子",
                     "你是我的助手吗", "今天天气怎么样", "怎么帮我"):
            with self.subTest(text=text):
                result=await main_router.choose(self.settings,
                    replace(web_msg,text=text),model=no_model)
                self.assertIsNone(result.agent_id)
        result=await main_router.choose(
            self.settings,replace(web_msg,text="帮我规划投资和预算"),model=no_model)
        self.assertEqual(result.agent_id,self.finance["id"])
        self.assertEqual(result.reason,"persona")

    async def test_delivered_once_even_after_restart_or_send_uncertainty(self):
        sender=AsyncMock(return_value="message-7")
        self.assertTrue(outbox.enqueue(self.settings,task_id="a"*32,
            channel="telegram",operator="1",chat_id="1",text="[Finance] untrusted result"))
        self.assertFalse(outbox.enqueue(self.settings,task_id="a"*32,
            channel="telegram",operator="1",chat_id="1",text="[Finance] untrusted result"))
        with patch("egress_taint.requires_approval",return_value=False):
            self.assertEqual(await outbox.consume_once(self.settings,"telegram",sender),1)
            self.assertEqual(await outbox.consume_once(self.settings,"telegram",sender),0)
        sender.assert_awaited_once_with("1","[Finance] untrusted result")
        with outbox._db(self.settings) as db:
            self.assertEqual(db.execute(
                "SELECT state FROM agent_channel_result_outbox WHERE task_id=?",
                ("a"*32,)).fetchone()[0],"sent")
        failing=AsyncMock(side_effect=TimeoutError())
        self.assertTrue(outbox.enqueue(self.settings,task_id="b"*32,
            channel="telegram",operator="1",chat_id="1",text="failed send"))
        with patch("egress_taint.requires_approval",return_value=False):
            self.assertEqual(await outbox.consume_once(self.settings,"telegram",failing),0)
            self.assertEqual(await outbox.consume_once(self.settings,"telegram",failing),0)
        failing.assert_awaited_once()

    async def test_egress_taint_blocks_automatic_result(self):
        sender=AsyncMock()
        outbox.enqueue(self.settings,task_id="c"*32,channel="feishu",
                       operator="ou_owner",chat_id="oc_private",text="private")
        with patch("egress_taint.requires_approval",return_value=True):
            self.assertEqual(await outbox.consume_once(self.settings,"feishu",sender),0)
        sender.assert_not_awaited()
        with outbox._db(self.settings) as db:
            self.assertEqual(db.execute(
                "SELECT state FROM agent_channel_result_outbox WHERE task_id=?",
                ("c"*32,)).fetchone()[0],"blocked")

if __name__ == "__main__":
    unittest.main()
