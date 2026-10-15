"""Durable agent-mailbox tests: atomic leases, identity, retries and taint."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
from threading import Barrier
import tempfile
import unittest
from unittest.mock import patch

import agent_context as ac
import agent_mailbox as mailbox
from agents import AgentStore, chat_id_for
from config import Settings


def _settings(root):
    return Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root / "workspace",
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        agents_enabled=True,
    )


class MailboxTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS": "1",
            "CONVEYOR_AGENT_CONTEXT": "1",
            "CONVEYOR_AGENT_MAILBOX": "1",
        })
        flags.start()
        self.addCleanup(flags.stop)
        self.settings = _settings(self.root)
        self.store = AgentStore(self.settings)
        self.a = self.store.create({"name": "Alpha", "tools_allowlist": []})
        self.b = self.store.create({"name": "Beta", "tools_allowlist": []})
        self.c = self.store.create({"name": "Gamma", "tools_allowlist": []})
        self.ctx_a = self._ctx(self.a["id"])
        self.ctx_b = self._ctx(self.b["id"])
        self.ctx_c = self._ctx(self.c["id"])

    def _ctx(self, agent_id):
        return ac.resolve(self.settings, "web", chat_id_for(agent_id),
                          "web-console")

    def _send(self, **kwargs):
        params = dict(sender=self.ctx_a, to_agent=self.b["id"],
                      kind="message", body="Untrusted memo: review this.",
                      idempotency_key="req-1", now=1000, deadline=2000)
        params.update(kwargs)
        with ac.bind(self.ctx_a):
            return mailbox.send(self.settings, **params)

    def _lease(self, ctx=None, **kwargs):
        ctx = ctx or self.ctx_b
        with ac.bind(ctx):
            return mailbox.lease(self.settings, recipient=ctx,
                                 worker_id="worker-1", now=1001, **kwargs)

    def _state(self, ctx, id_):
        with ac.bind(ctx):
            items = mailbox.list_for_agent(self.settings, recipient=ctx,
                                           now=1002)
        return next(x for x in items if x["id"] == id_)

    def test_idempotency_and_untrusted_never_approval(self):
        first = self._send()
        second = self._send()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(first["operator"], "web-console")
        self.assertEqual(first["from_agent"], self.a["id"])
        self.assertEqual(first["to_agent"], self.b["id"])
        self.assertEqual(first["trust_level"], "untrusted_agent_message")
        self.assertTrue(first["tainted"])
        self.assertFalse(first["approval_authority"])
        self.assertFalse(first["instruction_authority"])
        with self.assertRaises(mailbox.MailboxConflict):
            self._send(body="same key, different content")
        with self.assertRaises(mailbox.MailboxDenied):
            self._send(kind="approval", idempotency_key="bad-kind")
        with ac.bind(self.ctx_b):
            self.assertEqual(len(mailbox.list_for_agent(
                self.settings, recipient=self.ctx_b, now=1001)), 1)

    def test_concurrent_claim_only_one_worker_wins(self):
        first = self._send()
        barrier = Barrier(2)

        def claim(worker_id):
            barrier.wait(timeout=5)
            with ac.bind(self.ctx_b):
                return mailbox.lease(self.settings, recipient=self.ctx_b,
                                     worker_id=worker_id, now=1001)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("w-1", "w-2")))
        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["id"], first["id"])
        self.assertEqual(self._state(self.ctx_b, first["id"])["attempts"], 1)

    def test_lease_expiry_reclaimed_and_stale_ack_rejected(self):
        first = self._send()
        with ac.bind(self.ctx_b):
            leased = mailbox.lease(self.settings, recipient=self.ctx_b,
                                   worker_id="old", lease_seconds=5, now=1001)
            self.assertIsNotNone(leased)
            self.assertIsNone(mailbox.lease(
                self.settings, recipient=self.ctx_b, worker_id="new", now=1004))
            reclaimed = mailbox.lease(
                self.settings, recipient=self.ctx_b, worker_id="new",
                lease_seconds=5, now=1007)
            self.assertEqual(reclaimed["id"], first["id"])
            self.assertEqual(reclaimed["attempts"], 2)
            self.assertIsNone(mailbox.ack(
                self.settings, recipient=self.ctx_b, message_id=first["id"],
                worker_id="old", now=1008))
            done = mailbox.ack(self.settings, recipient=self.ctx_b,
                               message_id=first["id"], worker_id="new",
                               now=1008)
        self.assertEqual(done["status"], "done")
        self.assertIsNone(self._lease())

    def test_failed_retries_then_dead_and_deadline(self):
        first = self._send(max_attempts=2)
        with ac.bind(self.ctx_b):
            for i in (1, 2):
                item = mailbox.lease(self.settings, recipient=self.ctx_b,
                                     worker_id="w", now=1000+i)
                self.assertEqual(item["attempts"], i)
                result = mailbox.fail(self.settings, recipient=self.ctx_b,
                                      message_id=item["id"], worker_id="w",
                                      now=1000+i)
                self.assertEqual(result["status"],
                                 "failed" if i == 1 else "dead")
            self.assertIsNone(mailbox.lease(
                self.settings, recipient=self.ctx_b, worker_id="w", now=1010))
        self.assertEqual(self._state(self.ctx_b, first["id"])["status"], "dead")
        second = self._send(idempotency_key="deadline", deadline=1002)
        with ac.bind(self.ctx_b):
            self.assertEqual(mailbox.list_for_agent(
                self.settings, recipient=self.ctx_b, now=1003, status="dead")[0]["id"],
                second["id"])

    def test_expired_final_lease_becomes_dead(self):
        first = self._send(max_attempts=1)
        with ac.bind(self.ctx_b):
            mailbox.lease(self.settings, recipient=self.ctx_b,
                          worker_id="w", now=1000, lease_seconds=2)
            self.assertIsNone(mailbox.lease(
                self.settings, recipient=self.ctx_b, worker_id="w2", now=1003))
        self.assertEqual(self._state(self.ctx_b, first["id"])["status"], "dead")

    def test_wrong_operator_and_unbound_sender_rejected(self):
        with self.assertRaises(mailbox.MailboxDenied):
            mailbox.send(self.settings, sender=self.ctx_a, to_agent=self.b["id"],
                         kind="message", body="No binding", idempotency_key="x")
        other = ac.resolve(self.settings, "telegram", "owner-chat", "12345")
        with ac.bind(other):
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.send(self.settings, sender=other, to_agent=self.b["id"],
                             kind="message", body="Wrong operator",
                             idempotency_key="cross")
        first = self._send()
        with ac.bind(self.ctx_c):
            self.assertEqual(mailbox.list_for_agent(
                self.settings, recipient=self.ctx_c, now=1001), [])
            self.assertIsNone(mailbox.lease(
                self.settings, recipient=self.ctx_c, worker_id="w", now=1001))
        with ac.bind(self.ctx_a):
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.list_for_agent(self.settings, recipient=self.ctx_b)
        with ac.bind(self.ctx_b):
            self.assertIsNone(mailbox.ack(
                self.settings, recipient=self.ctx_b, message_id=first["id"],
                worker_id="not-the-lessee", now=1001))

    def test_archived_target_and_revoked_sender(self):
        self.store.archive(self.b["id"])
        with self.assertRaises(mailbox.MailboxDenied):
            self._send()
        with ac.bind(self.ctx_b):
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.list_for_agent(self.settings, recipient=self.ctx_b)
        self.store.archive(self.a["id"])
        with ac.bind(self.ctx_a):
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.send(self.settings, sender=self.ctx_a,
                             to_agent=self.c["id"], kind="message",
                             body="revoked", idempotency_key="revoked")

    def test_depth_parent_validation_and_body_limit(self):
        first = self._send()
        with ac.bind(self.ctx_b):
            child = mailbox.send(
                self.settings, sender=self.ctx_b, to_agent=self.a["id"],
                kind="task_result", body="Result: untrusted", idempotency_key="child",
                parent_message_id=first["id"], depth=1, now=1001, deadline=2000)
        self.assertEqual(child["depth"], 1)
        with ac.bind(self.ctx_a):
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.send(self.settings, sender=self.ctx_a,
                    to_agent=self.b["id"], kind="message", body="loop",
                    idempotency_key="grandchild", parent_message_id=child["id"],
                    depth=2, now=1002, deadline=2000)
            with self.assertRaises(mailbox.MailboxDenied):
                mailbox.send(self.settings, sender=self.ctx_a,
                    to_agent=self.b["id"], kind="message", body="false parent",
                    idempotency_key="wrong-parent", parent_message_id=first["id"],
                    depth=1, now=1002, deadline=2000)
        with self.assertRaises(mailbox.MailboxDenied):
            self._send(idempotency_key="oversize", body="汉"*3000)

    def test_flag_off_has_no_table_or_mailbox_actions(self):
        path = self.store.path
        def table_exists():
            with sqlite3.connect(str(path)) as conn:
                return conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='agent_mailbox'"
                ).fetchone() is not None
        self.assertFalse(table_exists())
        with patch.dict(os.environ, {"CONVEYOR_AGENT_MAILBOX": "0"}):
            with ac.bind(self.ctx_a):
                with self.assertRaises(mailbox.MailboxDenied):
                    mailbox.send(self.settings, sender=self.ctx_a,
                        to_agent=self.b["id"], kind="message",
                        body="off", idempotency_key="off")
        self.assertFalse(table_exists())
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CONTEXT": "0"}):
            with ac.bind(self.ctx_a):
                with self.assertRaises(mailbox.MailboxDenied):
                    mailbox.send(self.settings, sender=self.ctx_a,
                        to_agent=self.b["id"], kind="message",
                        body="off", idempotency_key="off2")
        self.assertFalse(table_exists())

    def test_taint_on_delivery_without_execution(self):
        first = self._send()
        with patch("egress_taint.mark_read") as mark:
            with ac.bind(self.ctx_b):
                leased = mailbox.lease(self.settings, recipient=self.ctx_b,
                                       worker_id="w", now=1001)
            mark.assert_called_once()
        self.assertEqual(leased["id"], first["id"])
        self.assertTrue(leased["tainted"])
        self.assertFalse(leased["approval_authority"])


if __name__ == "__main__":
    unittest.main()
