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
        with (patch("job_lanes.runner_for_chat",side_effect=lambda runner,*a:runner),
              patch("handlers.dispatch.try_resolve_confirmation",
                    new_callable=AsyncMock,return_value=False)):
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
                self.assertIn("[Conveyor]",p.replies[-1][1])
                self.assertIsNone(routing.bound(self.settings,channel,msg.chat_id))
                self.assertEqual(ac.resolve(self.settings,channel,msg.chat_id,msg.operator_id).agent_id,
                                 "default")

    async def test_unknown_archived_nonowner_and_group_denied(self):
        self.assertIn("Unknown", (await self.command(self.msg("/agent absent"))).replies[-1][1])
        self.store.archive(self.finance["id"])
        self.assertIn("Unknown",(await self.command(self.msg("/agent Finance"))).replies[-1][1])
        with self.assertRaises(PermissionError):
            routing.bind(self.settings,self.msg("/agent Helper",operator="2"),"Helper")
        group_port = Port()
        self.assertTrue(await routing.maybe_command(
            self.settings,self.msg("/agents",chat_type="group"),group_port))
        self.assertEqual(group_port.replies, [])
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

    async def test_feishu_command_behavior_unchanged(self):
        # 1. /agents on Feishu outputs English plain text
        port = await self.command(self.msg("/agents", channel="feishu"))
        self.assertIn("Available Agents:", port.replies[-1][1])
        self.assertIn("• main — default assistant", port.replies[-1][1])
        self.assertIn("Finance", port.replies[-1][1])

        # 2. /agent without argument on Feishu gives usage error
        port_empty = await self.command(self.msg("/agent", channel="feishu"))
        self.assertIn("Usage: /agent <name|id|main>", port_empty.replies[-1][1])

        # 3. /agent <name> on Feishu gives English acknowledgment
        port_bind = await self.command(self.msg("/agent Finance", channel="feishu"))
        self.assertIn("[Finance] Selected Agent.", port_bind.replies[-1][1])
        self.assertIn("Further messages use this Agent's history and tools.", port_bind.replies[-1][1])

    async def test_telegram_command_chinese_chooser_and_localized_ack(self):
        # 1. /agents on Telegram outputs Chinese chooser
        port_agents = await self.command(self.msg("/agents", channel="telegram"))
        self.assertIn("🤖 助手切换", port_agents.replies[-1][1])
        self.assertIn("当前助手: Conveyor (main)", port_agents.replies[-1][1])
        self.assertIn("Finance", port_agents.replies[-1][1])

        # 2. /agent without argument on Telegram outputs Chinese chooser
        port_agent_empty = await self.command(self.msg("/agent", channel="telegram"))
        self.assertIn("🤖 助手切换", port_agent_empty.replies[-1][1])
        self.assertIn("当前助手: Conveyor (main)", port_agent_empty.replies[-1][1])

        # 3. /agent <name> on Telegram gives Chinese acknowledgment
        port_bind = await self.command(self.msg("/agent Finance", channel="telegram"))
        self.assertIn("[Finance] 已切换至该助手。", port_bind.replies[-1][1])
        self.assertIn("后续消息将使用该助手的对话历史与工具。", port_bind.replies[-1][1])

    async def test_bind_exact_id_rejects_name_collision_with_archived_id(self):
        # Create agent A and archive it
        agent_a = self.store.create({"name": "TargetA", "persona": "original agent"})
        id_a = agent_a["id"]
        self.store.archive(id_a)

        # Create agent B whose name is agent A's ID
        agent_b = self.store.create({"name": id_a, "persona": "agent named after old id"})

        # Exact ID lookup must fail closed and NOT bind to agent B
        with self.assertRaises(ValueError):
            routing.bind(self.settings, self.msg("/agent"), id_a, exact_id=True)
        self.assertIsNone(routing.bound(self.settings, "telegram", "1"))

        # Exact ID lookup for live agent succeeds
        bound = routing.bind(self.settings, self.msg("/agent"), self.finance["id"], exact_id=True)
        self.assertEqual(bound["id"], self.finance["id"])
        self.assertEqual(routing.bound(self.settings, "telegram", "1"), self.finance["id"])

    async def test_agents_page_parsing_graceful_unicode_superscript(self):
        # Create enough agents for multiple pages
        for i in range(10):
            self.store.create({"name": f"Extra_{i:02d}"})

        # Unicode superscript ² would raise ValueError: invalid literal for int() if using .isdigit()
        port_unicode = await self.command(self.msg("/agents ²", channel="telegram"))
        self.assertIn("🤖 助手切换", port_unicode.replies[-1][1])
        self.assertIn("第 1/", port_unicode.replies[-1][1])

        # Valid ASCII page 2
        port_p2 = await self.command(self.msg("/agents 2", channel="telegram"))
        self.assertIn("🤖 助手切换", port_p2.replies[-1][1])
        self.assertIn("第 2/", port_p2.replies[-1][1])

        # Non-numeric or overflow page does not crash
        port_invalid = await self.command(self.msg("/agents abc", channel="telegram"))
        self.assertIn("🤖 助手切换", port_invalid.replies[-1][1])
        self.assertIn("第 1/", port_invalid.replies[-1][1])

    async def test_renamed_default_agent_reflected_in_chooser_and_ack(self):
        # Rename default agent in SQLite
        with self.store._connect() as conn:
            with conn:
                conn.execute("UPDATE agents SET name=? WHERE id='default'", ("CustomConveyor",))

        # Chooser display shows renamed default agent
        port_agents = await self.command(self.msg("/agents", channel="telegram"))
        self.assertIn("CustomConveyor", port_agents.replies[-1][1])
        self.assertIn("当前助手: CustomConveyor (main)", port_agents.replies[-1][1])

        # Ack shows renamed default agent
        port_main = await self.command(self.msg("/agent main", channel="telegram"))
        self.assertIn("[CustomConveyor] 已切换至该助手。", port_main.replies[-1][1])

    async def test_valid_id_name_collision_exact_id(self):
        # Agent 1 has unique ID and name
        agent_alpha = self.store.create({"name": "AlphaBot"})
        id_alpha = agent_alpha["id"]

        # Agent 2 has unique ID and its NAME is exactly Agent 1's ID
        agent_beta = self.store.create({"name": id_alpha})
        id_beta = agent_beta["id"]

        # Exact ID binding with id_alpha must bind Agent 1, not Agent 2
        bound = routing.bind(self.settings, self.msg("/agent"), id_alpha, exact_id=True)
        self.assertEqual(bound["id"], id_alpha)
        self.assertEqual(bound["name"], "AlphaBot")
        self.assertEqual(routing.bound(self.settings, "telegram", "1"), id_alpha)

        # Archive Agent 1: exact ID binding must fail closed and NOT fall back to Agent 2
        self.store.archive(id_alpha)
        with self.assertRaises(ValueError):
            routing.bind(self.settings, self.msg("/agent"), id_alpha, exact_id=True)

if __name__ == "__main__":
    unittest.main()
