"""DB-owner-safe startup recovery and atomic drain handshake."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from channel.types import InboundMessage
from handlers.job_queue import JobQueue, QueueJobState, _owner_alive, _process_identity
from scripts.deploy_db import deploy_fence


class NullPort:
    async def reply(self, *_args):
        return None


class QueueRecoveryFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(codex_memory_root=Path(self.temp.name),
                                        conveyor_max_pending_jobs=20,
                                        conveyor_event_retention_per_job=100)
        self.runner = SimpleNamespace(current_job=None)
        self.first = JobQueue()
        self.first.configure(self.settings, self.runner, recover=False)

    def _enqueue(self, text="job"):
        msg = InboundMessage(channel="telegram", operator_id="op", chat_id="chat",
                             message_id=None, text=text)
        result = asyncio.run(self.first.enqueue("run", text, msg, NullPort(), self.runner))
        self.assertTrue(result[0])
        return result[2]

    def test_other_process_start_keeps_live_owner(self):
        job = self._enqueue()
        asyncio.run(self.first.dequeue(require_idle=True))
        conn = self.first._get_conn()
        try:
            row = conn.execute("SELECT owner_pid, owner_identity FROM queued_jobs WHERE id=?", (job.id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["owner_pid"], os.getpid())
        if _process_identity(os.getpid()) is None:
            self.skipTest("Linux /proc owner identity unavailable")
        self.assertTrue(_owner_alive(row["owner_pid"], row["owner_identity"]))
        second = JobQueue()
        second.configure(self.settings, self.runner, recover=True)
        self.assertEqual(asyncio.run(second.get_job(job.id)).state, QueueJobState.RUNNING)

    def test_recover_only_verifiably_dead_owner(self):
        job = self._enqueue()
        asyncio.run(self.first.dequeue(require_idle=True))
        conn = self.first._get_conn()
        try:
            with conn:
                conn.execute("UPDATE queued_jobs SET owner_pid=?, owner_identity=? WHERE id=?",
                             (999999999, "dead-boot:123", job.id))
        finally:
            conn.close()
        second = JobQueue()
        second.configure(self.settings, self.runner, recover=True)
        self.assertEqual(asyncio.run(second.get_job(job.id)).state, QueueJobState.INTERRUPTED)

    def test_unknown_legacy_owner_never_interrupted(self):
        job = self._enqueue()
        asyncio.run(self.first.dequeue(require_idle=True))
        conn = self.first._get_conn()
        try:
            with conn:
                conn.execute("UPDATE queued_jobs SET owner_pid=NULL, owner_identity=NULL WHERE id=?", (job.id,))
        finally:
            conn.close()
        second = JobQueue()
        second.configure(self.settings, self.runner, recover=True)
        self.assertEqual(asyncio.run(second.get_job(job.id)).state, QueueJobState.RUNNING)

    def test_recycled_pid_is_dead_not_owned(self):
        identity = _process_identity(os.getpid())
        if identity is None:
            self.skipTest("Linux /proc owner identity unavailable")
        self.assertFalse(_owner_alive(os.getpid(), "different-boot:9876"))
        self.assertIsNone(_owner_alive(None, None))

    def test_atomic_deploy_fence_serializes_dequeue(self):
        job = self._enqueue()
        path = self.first._db_path()
        token = "a" * 32
        deploy_fence(path, token)
        second = JobQueue()
        second.configure(self.settings, self.runner, recover=True)
        self.assertFalse(second.can_start(job.id))
        self.assertIsNone(asyncio.run(second.dequeue(require_idle=True)))
        with self.assertRaisesRegex(RuntimeError, "another deployment"):
            deploy_fence(path, "b" * 32)
        with self.assertRaisesRegex(RuntimeError, "token mismatch"):
            deploy_fence(path, "b" * 32, release=True)
        self.assertIsNone(asyncio.run(self.first.dequeue(require_idle=False)))
        deploy_fence(path, token, release=True)
        self.assertEqual(asyncio.run(second.dequeue(require_idle=True)).id, job.id)

    def test_fence_rejects_any_outstanding_queue_or_running_job(self):
        self._enqueue()
        with self.assertRaisesRegex(RuntimeError, "not idle"):
            deploy_fence(self.first._db_path(), "c" * 32)
        conn = self.first._get_conn()
        try:
            self.assertIsNone(conn.execute("SELECT value FROM queue_metadata WHERE key='deploy_fence'").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
