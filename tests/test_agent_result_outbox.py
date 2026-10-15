"""Crash-safe acknowledged routed result outbox and Agent kill controls."""
from __future__ import annotations

from dataclasses import replace
import asyncio
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
import agent_channel_results as adapter
import agent_security
import agent_security_controls
import main_router_results
from agents import AgentStore, chat_id_for
from config import Settings
from channel.types import InboundMessage


def _settings(root):
    return replace(Settings(
        telegram_bot_token="test", telegram_allowed_user_id=1,
        codex_workspace_root=root/"workspace",codex_bin="codex",
        codex_task_root=root/"tasks",codex_model=None,
        codex_timeout_seconds=30,codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,codex_memory_root=root,
        user_timezone="UTC",agents_enabled=True),
        chat_mode="auto", chat_api_key="test", chat_model="mock",
        lark_allowed_open_id="ou_owner")


class ResultOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = _settings(Path(temp.name))
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS":"1",
            "CONVEYOR_AGENT_CONTEXT":"1",
            "CONVEYOR_AGENT_MAILBOX":"1",
            "CONVEYOR_AGENT_DELEGATION":"1",
            "CONVEYOR_MAIN_ROUTER":"1",
            "CONVEYOR_AGENT_CHANNEL_ROUTING":"1",
            "CONVEYOR_AGENTS_KILL":"0"})
        flags.start()
        self.addCleanup(flags.stop)
        self.store = AgentStore(self.settings)
        self.target_agent = self.store.create({"name":"Finance",
            "tools_allowlist":["notes.search"]})
        self.source = ac.resolve(self.settings,"web",chat_id_for("default"),"web-console")
        self.target = ac.resolve(self.settings,"web",
                                 chat_id_for(self.target_agent["id"]),"web-console")

    def register_and_queue(self, token, *, channel="telegram"):
        owner = "1" if channel == "telegram" else "ou_owner"
        chat = "1" if channel == "telegram" else "oc_private"
        main_router_results.register(
            self.settings,key="delegate-"+token,
            agent_id=self.target_agent["id"],agent_name="Finance",
            channel=channel, chat_id=chat,operator=owner)
        arg = delegation.approval_arg(json.dumps({
            "to":self.target_agent["id"],"task":"Analyze balances",
            "tools_subset":[]}), token)
        with ac.bind(self.source), delegation.approved_token(token):
            queued = delegation.enqueue(
                self.settings,arg,channel="web",
                chat_id=chat_id_for("default"),operator_id="web-console",
                approved=True)
        self.assertIn("agent task queued:",queued)
        return queued.rsplit(": ",1)[1]

    def commit_result(self, token, *, channel="telegram", text="Budget ready"):
        task_id = self.register_and_queue(token,channel=channel)
        with ac.bind(self.target):
            claim = queue.claim(self.settings,self.target)
            self.assertIsNotNone(claim)
            task, worker = claim
            self.assertEqual(task["id"],task_id)
            self.assertTrue(queue.complete(
                self.settings,self.target,task,worker,text))
        return task

    def records(self):
        with sqlite3.connect(str(self.store.path)) as db:
            db.row_factory=sqlite3.Row
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='agent_result_notifications'"
            ).fetchone():
                return []
            return [dict(x) for x in db.execute(
                "SELECT * FROM agent_result_notifications ORDER BY created_at,id")]

    async def test_crash_after_complete_before_delivery_recovers_from_sqlite(self):
        task = self.commit_result("recover",channel="telegram")
        # Intentionally never call main_router_results.deliver. Simulate a
        # process restart between commit and platform send.
        self.assertEqual(len(self.records()),1)
        pending = self.records()[0]
        self.assertEqual(pending["state"],"pending")
        self.assertEqual(pending["task_id"],task["id"])
        self.assertEqual(pending["channel"],"telegram")
        self.assertIn("[result:"+pending["result_id"][:12]+"]",pending["text"])
        send = AsyncMock(return_value="message-42")
        with patch("egress_taint.requires_approval",return_value=False):
            self.assertEqual(await adapter.consume_once(self.settings,"telegram",send),1)
            self.assertEqual(await adapter.consume_once(self.settings,"telegram",send),0)
        send.assert_awaited_once()
        self.assertEqual(self.records()[0]["platform_message_id"],"message-42")
        self.assertEqual(self.records()[0]["state"],"sent")

    async def test_failed_send_retries_with_backoff_no_double_success(self):
        self.commit_result("retry",channel="feishu")
        sender = AsyncMock(side_effect=[TimeoutError("uncertain"),"feishu-ack"])
        with patch("egress_taint.requires_approval",return_value=False):
            self.assertEqual(await outbox.consume_once(self.settings,"feishu",sender),0)
            row = self.records()[0]
            self.assertEqual(row["state"],"retry")
            self.assertEqual(row["attempts"],1)
            self.assertGreater(row["next_attempt"],row["updated_at"])
            # No tight-loop re-send before backoff expires.
            self.assertIsNone(await outbox.consume_once(self.settings,"feishu",sender))
            with outbox._db(self.settings) as db:
                db.execute("UPDATE agent_result_notifications SET next_attempt=0")
            self.assertEqual(await outbox.consume_once(self.settings,"feishu",sender),1)
            self.assertIsNone(await outbox.consume_once(self.settings,"feishu",sender))
        self.assertEqual(sender.await_count,2)
        row=self.records()[0]
        self.assertEqual((row["state"],row["attempts"],
                          row["platform_message_id"]),("sent",2,"feishu-ack"))

    async def test_kill_pauses_without_dropping_then_resumes(self):
        self.commit_result("paused")
        sender=AsyncMock(return_value="ok")
        self.assertTrue(agent_security.set_kill(
            self.settings,enabled=True,operator="web-console",source="web"))
        self.assertTrue(outbox.configured(self.settings))
        with patch("egress_taint.requires_approval",return_value=False):
            self.assertEqual(await outbox.consume_once(
                self.settings,"telegram",sender),0)
            self.assertEqual(self.records()[0]["state"],"pending")
            self.assertFalse(agent_security.set_kill(
                self.settings,enabled=False,operator="web-console",source="web"))
            self.assertEqual(await outbox.consume_once(
                self.settings,"telegram",sender),1)
        sender.assert_awaited_once()
        self.assertEqual(self.records()[0]["state"],"sent")

    async def test_crash_claim_lease_is_retried_then_max_attempts_dead(self):
        self.commit_result("lease")
        claimed=outbox._claim(self.settings,"telegram")
        self.assertEqual(claimed["attempts"],1)
        self.assertIsNone(outbox._claim(self.settings,"telegram"))
        with outbox._db(self.settings) as db:
            db.execute("UPDATE agent_result_notifications SET lease_expires=0")
        claimed2=outbox._claim(self.settings,"telegram")
        self.assertEqual(claimed2["attempts"],2)
        with outbox._db(self.settings) as db:
            db.execute("""UPDATE agent_result_notifications
                SET attempts=?,lease_expires=0""",(outbox.MAX_ATTEMPTS,))
        self.assertIsNone(outbox._claim(self.settings,"telegram"))
        self.assertEqual(self.records()[0]["state"],"dead")
        with sqlite3.connect(str(self.store.path)) as db:
            self.assertGreater(db.execute(
                "SELECT COUNT(*) FROM agent_security_audit WHERE event='deny'"
            ).fetchone()[0],0)

    async def test_egress_refusal_does_not_send_and_retries_or_dead(self):
        self.commit_result("egress")
        sender=AsyncMock()
        with patch("egress_taint.requires_approval",return_value=True):
            self.assertEqual(await outbox.consume_once(
                self.settings,"telegram",sender),0)
        sender.assert_not_awaited()
        self.assertEqual(self.records()[0]["state"],"retry")
        with outbox._db(self.settings) as db:
            db.execute("UPDATE agent_result_notifications SET attempts=?,next_attempt=0",
                       (outbox.MAX_ATTEMPTS-1,))
        with patch("egress_taint.requires_approval",return_value=True):
            self.assertEqual(await outbox.consume_once(
                self.settings,"telegram",sender),0)
        self.assertEqual(self.records()[0]["state"],"dead")

    async def test_flags_off_no_schema_changes_or_kill_endpoint(self):
        with patch.dict(os.environ,{"CONVEYOR_AGENT_CHANNEL_ROUTING":"0"}):
            self.assertFalse(outbox.configured(self.settings))
            self.commit_result("disabled")
            self.assertEqual(self.records(),[])
            self.assertIsNone(await outbox.consume_once(
                self.settings,"telegram",AsyncMock()))
        with patch.dict(os.environ,{"CONVEYOR_NAMED_AGENTS":"0"}):
            self.assertFalse(agent_security_controls.available(self.settings))
            message=InboundMessage(channel="telegram", operator_id="1",
                chat_id="1",message_id="cmd",text="/agentkill on",chat_type="p2p")
            port=AsyncMock()
            self.assertTrue(await agent_security_controls.owner_command(
                self.settings,message,port))
            port.reply.assert_not_awaited()
            self.assertFalse(agent_security.killed(self.settings))
        self.assertTrue(agent_security_controls.available(self.settings))

    async def test_duplicate_result_outbox_row_and_retention(self):
        task=self.commit_result("unique")
        with outbox._db(self.settings) as db:
            row=db.execute(
                "SELECT result_id FROM agent_result_notifications").fetchone()
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM agent_result_notifications").fetchone()[0],1)
            self.assertTrue(outbox.enqueue_committed(
                db,self.settings,task,row["result_id"],"Budget ready"))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM agent_result_notifications").fetchone()[0],1)
            db.execute(
                "UPDATE agent_result_notifications SET state='sent',updated_at=0")
        self.assertEqual(outbox.purge(self.settings),1)
        self.assertEqual(self.records(),[])


if __name__ == "__main__":
    unittest.main()
