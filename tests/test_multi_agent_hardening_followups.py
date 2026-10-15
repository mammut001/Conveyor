"""PR145 hot kill, live-result retention and concurrent admission regressions."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_context as ac
import agent_mailbox as mailbox
import agent_delegation_queue as q
import agent_delegation_worker as worker
import agent_security as security
import main_router
from agents import AgentStore, WEB_OPERATOR, chat_id_for
from config import Settings


class SecurityFollowups(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.cfg = Settings(
            telegram_bot_token="test", telegram_allowed_user_id=1,
            codex_workspace_root=root/"workspace", codex_bin="codex",
            codex_task_root=root/"tasks", codex_model=None,
            codex_timeout_seconds=30, codex_retry_429_delays_seconds=(),
            telegram_progress_seconds=1, codex_memory_root=root,
            user_timezone="UTC", agents_enabled=True)
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS":"1", "CONVEYOR_AGENT_CONTEXT":"1",
            "CONVEYOR_AGENT_MAILBOX":"1", "CONVEYOR_AGENT_DELEGATION":"1",
            "CONVEYOR_MAIN_ROUTER":"1", "CONVEYOR_AGENTS_KILL":"0"})
        flags.start()
        self.addCleanup(flags.stop)
        self.store = AgentStore(self.cfg)
        self.a = self.store.create({"name":"Alpha","tools_allowlist":["agent.delegate"]})
        self.b = self.store.create({"name":"Beta","tools_allowlist":[]})

    def ctx(self, agent):
        return ac.resolve(self.cfg, "web", chat_id_for(agent["id"]), WEB_OPERATOR)

    def send(self, key, *, kind="message", now=None):
        sender = self.ctx(self.a)
        with ac.bind(sender):
            return mailbox.send(self.cfg, sender=sender, to_agent=self.b["id"],
                kind=kind, body="untrusted", idempotency_key=key, now=now,
                deadline=None if now is None else now+3600)

    def status(self, task_id):
        with sqlite3.connect(str(self.store.path)) as db:
            return db.execute("SELECT status FROM agent_mailbox WHERE id=?",
                              (task_id,)).fetchone()[0]

    async def test_runtime_kill_and_unkill_same_running_worker(self):
        initial = self.send("kill-leased", kind="task")
        target = self.ctx(self.b)
        with ac.bind(target):
            row, owner = q.claim(self.cfg, target)
        self.assertEqual(row["status"], "leased")
        security.set_kill(self.cfg, enabled=True, operator=WEB_OPERATOR, source="web")
        self.assertTrue(security.killed(self.cfg))
        self.assertFalse(main_router.enabled(self.cfg))
        with self.assertRaises(mailbox.MailboxDenied):
            self.send("kill-denied", kind="task")
        resumed = asyncio.Event()
        async def process(settings):
            with ac.bind(target):
                found = q.claim(settings, target)
                if found:
                    task, lease = found
                    self.assertTrue(q.complete(settings, target, task, lease, "done"))
                    resumed.set()
            return 1
        with patch("agent_delegation_worker.poll_once", side_effect=process):
            poller = asyncio.create_task(worker.Worker(self.cfg).run())
            try:
                await asyncio.sleep(.65)
                self.assertFalse(poller.done())
                self.assertEqual(self.status(initial["id"]), "dead")
                with ac.bind(target):
                    self.assertFalse(q.complete(self.cfg, target, row, owner, "late"))
                security.set_kill(self.cfg, enabled=False,
                                  operator=WEB_OPERATOR, source="web")
                self.assertFalse(security.killed(self.cfg))
                following = self.send("unkill-resumed", kind="task")
                await asyncio.wait_for(resumed.wait(), timeout=3)
                self.assertEqual(self.status(following["id"]), "done")
                self.assertFalse(poller.done())
            finally:
                poller.cancel()
                await asyncio.gather(poller, return_exceptions=True)
        reasons = {r["reason"] for r in security.audit_for_agent(
            self.cfg, "default", WEB_OPERATOR)}
        self.assertTrue({"kill_on", "kill_off"} <= reasons)

    async def test_env_override_cannot_be_cleared_by_runtime_control(self):
        with patch.dict(os.environ, {"CONVEYOR_AGENTS_KILL":"1"}):
            security.set_kill(self.cfg, enabled=False,
                              operator=WEB_OPERATOR, source="web")
            self.assertTrue(security.killed(self.cfg))
        self.assertFalse(security.killed(self.cfg))

    async def test_old_pending_of_every_kind_survives_retention(self):
        old = time.time()-25*86400
        ids = []
        for kind in ("message", "task", "result", "task_result"):
            for state in ("queued", "leased"):
                item = self.send(f"retain-{kind}-{state}", kind=kind, now=old)
                ids.append(item["id"])
                with sqlite3.connect(str(self.store.path)) as db:
                    db.execute("UPDATE agent_mailbox SET status=? WHERE id=?",
                               (state, item["id"]))
        with patch.dict(os.environ, {"CONVEYOR_AGENT_RETENTION_DAYS":"14"}):
            self.assertEqual(security.purge_once(self.cfg, now=time.time(), batch=100), 0)
        with sqlite3.connect(str(self.store.path)) as db:
            current = {r[0] for r in db.execute("SELECT id FROM agent_mailbox")}
        self.assertEqual(set(ids), current)

    async def test_twenty_parallel_sends_admit_exactly_five(self):
        gate = threading.Barrier(20)
        def submit(index):
            sender = self.ctx(self.a)
            gate.wait(timeout=10)
            with ac.bind(sender):
                try:
                    return mailbox.send(self.cfg, sender=sender,
                        to_agent=self.b["id"], kind="message",
                        body="untrusted", idempotency_key=f"parallel-{index}")
                except ValueError as exc:
                    return str(exc)
        with patch.dict(os.environ, {"CONVEYOR_AGENT_RATE_PER_HOUR":"5"}):
            with ThreadPoolExecutor(max_workers=20) as pool:
                output = list(pool.map(submit, range(20)))
        self.assertEqual(sum(isinstance(item, dict) for item in output), 5)
        self.assertEqual(sum("rate limit" in item for item in output
                             if isinstance(item, str)), 15)


if __name__ == "__main__":
    unittest.main()
