"""Multi-agent final security regression: admission, kill, retention, audit."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_context as ac
import agent_mailbox as mailbox
import agent_delegation as delegation
import agent_delegation_worker as worker
import agent_channel_routing as channel
import agent_security as security
import main_router
import web_agent_screens as screens
from agents import AgentStore, WEB_OPERATOR, chat_id_for
from config import Settings
from channel.types import InboundMessage
from handlers.chat_tools import build_tool_schemas, _get_tool_spec
from handlers.tools.runner import run_tool


def settings(root):
    return Settings(
        telegram_bot_token="test", telegram_allowed_user_id=1,
        codex_workspace_root=root/"workspace", codex_bin="codex",
        codex_task_root=root/"tasks", codex_model=None,
        codex_timeout_seconds=30, codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", agents_enabled=True)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg=settings(Path(tmp.name))
        flag=patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS":"1", "CONVEYOR_AGENT_CONTEXT":"1",
            "CONVEYOR_AGENT_MAILBOX":"1", "CONVEYOR_AGENT_DELEGATION":"1",
            "CONVEYOR_MAIN_ROUTER":"1",
            "CONVEYOR_AGENT_CHANNEL_ROUTING":"1",
            "CONVEYOR_AGENTS_KILL":"0"})
        flag.start(); self.addCleanup(flag.stop)
        self.store=AgentStore(self.cfg)
        self.a=self.store.create({"name":"Alpha", "tools_allowlist":["agent.delegate","notes.search"]})
        self.b=self.store.create({"name":"Beta", "tools_allowlist":["notes.search"]})
        self.c=self.store.create({"name":"Gamma", "tools_allowlist":["notes.search"]})

    def ctx(self, info):
        return ac.resolve(self.cfg,"web",chat_id_for(info["id"]),WEB_OPERATOR)

    def send(self, key, *, target=None, sender=None, kind="message", now=None, parent=None):
        target=target or self.b
        sender=sender or self.a
        with ac.bind(self.ctx(sender)):
            return mailbox.send(
                self.cfg, sender=self.ctx(sender), to_agent=target["id"],
                kind=kind, body="untrusted data", idempotency_key=key,
                now=now, deadline=None if now is None else now+1000,
                parent_message_id=parent, depth=1 if parent else 0)

    def rows(self):
        with sqlite3.connect(str(self.store.path)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM agent_mailbox")]

    async def test_operator_rate_limit_is_atomic_and_duplicate_is_free(self):
        with patch.dict(os.environ, {"CONVEYOR_AGENT_RATE_PER_HOUR":"2"}):
            a=self.send("one")
            self.assertEqual(self.send("one")["id"],a["id"])
            self.send("two",target=self.c)
            with self.assertRaisesRegex(ValueError,"rate limit"):
                self.send("three",target=self.c)
            self.assertEqual(len(self.rows()),2)
        self.assertTrue(security.audit_for_agent(self.cfg,self.b["id"],WEB_OPERATOR))

    async def test_recipient_queue_cap_does_not_block_different_agent(self):
        with patch.dict(os.environ, {"CONVEYOR_AGENT_MAX_PENDING":"1"}):
            self.send("first")
            with self.assertRaisesRegex(ValueError,"queue full"):
                self.send("second")
            self.send("third",target=self.c)
            self.assertEqual(len(self.rows()),2)

    async def test_parent_child_retention_and_bounded_purge(self):
        # Old terminal parent and child; a queued current message survives.
        now=time.time()
        old=now-25*86400
        parent=self.send("old-parent",now=old)
        child=self.send("old-child",sender=self.b,target=self.a,
                        kind="task_result",parent=parent["id"],now=old+1)
        self.send("new-parent")
        with sqlite3.connect(str(self.store.path)) as db:
            db.execute("UPDATE agent_mailbox SET status='done' WHERE id IN (?,?)",
                       (parent["id"],child["id"]))
        with patch.dict(os.environ, {"CONVEYOR_AGENT_RETENTION_DAYS":"14"}):
            self.assertEqual(security.purge_once(self.cfg,now=now,batch=1),1)
            remaining={r["id"] for r in self.rows()}
            self.assertIn(parent["id"],remaining)
            self.assertNotIn(child["id"],remaining)
            self.assertEqual(security.purge_once(self.cfg,now=now,batch=5),1)
            remaining={r["id"] for r in self.rows()}
            self.assertNotIn(parent["id"],remaining)
            self.assertIn(next(r["id"] for r in self.rows()),remaining)
        with sqlite3.connect(str(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])

    async def test_identity_audit_redacted_and_operator_scoped(self):
        self.send("audit-one")
        security.audit(self.cfg,operator=WEB_OPERATOR,event="route",
                       agent_id=self.b["id"],reason="persona")
        security.audit(self.cfg,operator=WEB_OPERATOR,event="deny",
                       agent_id=self.b["id"],reason="SYSTEM: SECRET BODY!!!")
        rows=security.audit_for_agent(self.cfg,self.b["id"],WEB_OPERATOR)
        self.assertEqual({r["event"] for r in rows},{"route","delegate","deny"})
        self.assertIn("denied",{r["reason"] for r in rows})
        self.assertFalse(any("BODY" in str(r) or "SECRET" in str(r) for r in rows))
        self.assertEqual(security.audit_for_agent(self.cfg,self.b["id"],"another-operator"),[])
        self.assertEqual(screens.list_items(self.cfg,self.b["id"],"audit"),rows)
        with ac.bind(self.ctx(self.a)), self.assertRaises(PermissionError):
            screens.list_items(self.cfg,self.b["id"],"audit")

    async def test_hot_kill_blocks_routing_channel_and_tool_enqueues_but_stop_works(self):
        src=self.ctx(self.a)
        with patch.dict(os.environ, {"CONVEYOR_AGENTS_KILL":"1"}):
            self.assertTrue(security.killed())
            self.assertFalse(mailbox.enabled(self.cfg))
            self.assertFalse(delegation.enabled(self.cfg))
            self.assertFalse(main_router.enabled(self.cfg))
            self.assertFalse(channel.enabled(self.cfg))
            self.assertIsNone(channel.bound(self.cfg,"telegram","1"))
            self.assertIsNone(_get_tool_spec("agent.message",self.cfg))
            self.assertIsNone(_get_tool_spec("agent.delegate",self.cfg))
            offered={x["function"]["name"] for x in build_tool_schemas(
                self.cfg,channel="web", chat_id=chat_id_for(self.a["id"]),operator_id=WEB_OPERATOR)}
            self.assertEqual(offered,set())
            self.assertIn("disabled",await run_tool(
                self.cfg,"notes.search","{}",
                operator_id=WEB_OPERATOR,channel="web",
                chat_id=chat_id_for(self.a["id"])))
            with ac.bind(src),self.assertRaises(mailbox.MailboxDenied):
                self.send("stopped")
            self.assertIsNone(worker.start(asyncio.get_running_loop(),self.cfg))
        # Existing live tasks can still be cancelled with kill enabled.
        self.send("post-kill",kind="task")
        with patch.dict(os.environ, {"CONVEYOR_AGENTS_KILL":"1"}):
            self.assertEqual(worker.cancel_all(self.cfg),1)
        self.assertEqual(self.rows()[0]["status"],"dead")

    async def test_worker_runtime_hot_kill_cancels_lease_and_stops(self):
        self.send("live-kill",kind="task")
        import agent_delegation_queue as q
        target=self.ctx(self.b)
        with ac.bind(target):
            record,wid=q.claim(self.cfg,target)
        self.assertEqual(record["status"],"leased")
        with patch.dict(os.environ,{"CONVEYOR_AGENTS_KILL":"1"}):
            obj=worker.Worker(self.cfg)
            await asyncio.wait_for(obj.run(),timeout=2)
        self.assertEqual(self.rows()[0]["status"],"dead")
        with ac.bind(target):
            self.assertFalse(q.complete(self.cfg,target,record,wid,"late result"))

    async def test_default_off_introduces_no_security_schema(self):
        with patch.dict(os.environ,{"CONVEYOR_AGENT_MAILBOX":"0",
                                     "CONVEYOR_AGENT_DELEGATION":"0",
                                     "CONVEYOR_MAIN_ROUTER":"0"}):
            self.assertIsNone(security.start(asyncio.get_running_loop(),self.cfg))
            self.assertFalse(mailbox.enabled(self.cfg))
            self.assertFalse(delegation.enabled(self.cfg))
            with sqlite3.connect(str(self.store.path)) as db:
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='agent_security_audit'").fetchone())


if __name__=="__main__":
    unittest.main()
