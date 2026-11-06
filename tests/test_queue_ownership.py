from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from channel.types import InboundMessage
from handlers.job_queue import JobQueue, QueueJobState


class FakeOutbound:
    async def reply(self, _msg, _text):
        return None


class QueueOwnershipTests(unittest.TestCase):
    def test_completion_without_start_callback_does_not_claim_next_job(self):
        """A status-only process must leave executable work in QUEUED state."""
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                codex_memory_root=Path(temp_dir),
                conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100,
            )
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue(max_length=10)
            queue.configure(settings, runner, recover=False)
            self.assertIsNone(queue._start_callback)

            message = InboundMessage(
                channel="web",
                operator_id="web",
                chat_id="web-session",
                message_id=None,
                text="test",
            )
            port = FakeOutbound()

            async def scenario():
                _, _, first = await queue.enqueue("run", "first", message, port, runner)
                _, _, second = await queue.enqueue("run", "second", message, port, runner)
                claimed = await queue.dequeue(require_idle=True)
                self.assertEqual(claimed.id, first.id)

                await queue.on_job_completed(queue_job_id=first.id)
                return await queue.get_job(second.id)

            second_after_completion = asyncio.run(scenario())
            self.assertEqual(second_after_completion.state, QueueJobState.QUEUED)
            self.assertEqual(second_after_completion.position, 1)

    def test_completion_with_start_callback_claims_and_starts_next_job(self):
        """An execution owner keeps the established automatic FIFO behavior."""
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(
                codex_memory_root=Path(temp_dir),
                conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100,
            )
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue(max_length=10)
            queue.configure(settings, runner, recover=False)
            started = []

            async def start(job):
                started.append(job.id)

            queue.set_start_callback(start)
            message = InboundMessage(
                channel="web",
                operator_id="web",
                chat_id="web-session",
                message_id=None,
                text="test",
            )
            port = FakeOutbound()

            async def scenario():
                _, _, first = await queue.enqueue("run", "first", message, port, runner)
                _, _, second = await queue.enqueue("run", "second", message, port, runner)
                claimed = await queue.dequeue(require_idle=True)
                self.assertEqual(claimed.id, first.id)
                await queue.on_job_completed(queue_job_id=first.id)
                return second, await queue.get_job(second.id)

            second, second_after_completion = asyncio.run(scenario())
            self.assertEqual(second_after_completion.state, QueueJobState.RUNNING)
            self.assertEqual(started, [second.id])

class QueueSafetyTests(unittest.TestCase):
    def _queue(self, root):
        settings = SimpleNamespace(
            codex_memory_root=Path(root),
            conveyor_max_pending_jobs=10,
            conveyor_event_retention_per_job=100,
        )
        runner = SimpleNamespace(current_job=None)
        queue = JobQueue(max_length=10)
        queue.configure(settings, runner, recover=False)
        return queue, runner

    @staticmethod
    def _msg(chat):
        return InboundMessage(channel="web", operator_id="web", chat_id=chat, message_id=None, text="t")

    def test_queue_resume_does_not_complete_a_running_job(self):
        with tempfile.TemporaryDirectory() as root:
            queue, runner = self._queue(root)

            async def scenario():
                _, _, first = await queue.enqueue("run", "first", self._msg("a"), FakeOutbound(), runner)
                claimed = await queue.dequeue(require_idle=True)
                self.assertEqual(claimed.id, first.id)
                await queue.pause()
                await queue.resume()
                await queue.start_next()          # what /queue resume now does
                await queue.on_job_completed()    # legacy no-id call: must be a no-op
                return await queue.get_job(first.id)

            self.assertEqual(asyncio.run(scenario()).state, QueueJobState.RUNNING)

    def test_foreign_queued_job_keeps_its_own_chat(self):
        """An older job this process did not enqueue must not run under the new submitter's chat."""
        import handlers.jobs as jobs
        from unittest.mock import AsyncMock, patch
        from runner.types import JobMode

        with tempfile.TemporaryDirectory() as root:
            other, _ = self._queue(root)       # e.g. the Telegram process
            queue, runner = self._queue(root)  # this (web) process
            runner.settings = SimpleNamespace(
                conveyor_max_jobs_per_hour=100, conveyor_max_pending_jobs=10,
                codex_memory_root=Path(root))

            async def scenario():
                _, _, foreign = await other.enqueue("run", "foreign prompt", self._msg("chatY"), FakeOutbound(), runner)
                started, executed = [], []

                async def fake_callback(job):
                    started.append(job)

                async def fake_execute(msg, port, runner_, mode, prompt, **kw):
                    executed.append((msg.chat_id, prompt))

                with patch("handlers.job_queue.get_job_queue", return_value=queue), \
                        patch("agent_context.enabled", return_value=False), \
                        patch.object(jobs, "_start_queued_job_callback", new=fake_callback), \
                        patch.object(jobs, "_execute_codex_job", new=fake_execute):
                    ok, _text, mine = await jobs.submit_codex_job(
                        self._msg("chatZ"), FakeOutbound(), runner, mode=JobMode.RUN,
                        prompt="my prompt", wait=True)
                return ok, foreign, mine, started, executed

            ok, foreign, mine, started, executed = asyncio.run(scenario())
            self.assertTrue(ok)
            self.assertEqual([j.id for j in started], [foreign.id])
            self.assertEqual(executed, [])  # nothing ran under chatZ's message
            self.assertNotEqual(mine.id, foreign.id)

class QueueMemoryReferenceTests(unittest.TestCase):
    def test_handles_of_jobs_closed_elsewhere_are_released(self):
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(
                codex_memory_root=Path(root), conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100)
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue(max_length=10)
            queue.configure(settings, runner, recover=False)
            msg = InboundMessage(channel="web", operator_id="w", chat_id="c", message_id=None, text="t")

            async def scenario():
                _, _, live = await queue.enqueue("run", "live", msg, FakeOutbound(), runner)
                for i in range(queue._MEMORY_REFERENCE_PRUNE_AT):
                    queue._memory_references[f"gone-{i}"] = {"msg": msg}
                queue._prune_memory_references()
                return live

            live = asyncio.run(scenario())
            self.assertEqual(set(queue._memory_references), {live.id})

    def test_completion_releases_handles_even_if_row_was_closed_by_another_process(self):
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(
                codex_memory_root=Path(root), conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100)
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue(max_length=10)
            queue.configure(settings, runner, recover=False)
            msg = InboundMessage(channel="web", operator_id="w", chat_id="c", message_id=None, text="t")

            async def scenario():
                _, _, job = await queue.enqueue("run", "x", msg, FakeOutbound(), runner)
                await queue.cancel(job.id)               # closed without this completion path
                await queue.on_job_completed(queue_job_id=job.id)
                return job

            job = asyncio.run(scenario())
            self.assertNotIn(job.id, queue._memory_references)

class QueueCancelRaceTests(unittest.TestCase):
    def test_cancel_does_not_mark_a_job_another_process_just_started(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(
                codex_memory_root=Path(root), conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100)
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue(max_length=10)
            queue.configure(settings, runner, recover=False)
            msg = InboundMessage(channel="web", operator_id="w", chat_id="c", message_id=None, text="t")
            real_get_conn = queue._get_conn

            class RacingConn:
                """Between the cancel's SELECT and UPDATE, another process claims the job."""
                def __init__(self, conn, job_id):
                    self._conn, self._job_id, self._raced = conn, job_id, False
                def execute(self, sql, *args):
                    if not self._raced and "SET state = 'cancelled'" in sql:
                        self._raced = True
                        other = real_get_conn()
                        with other:
                            other.execute("UPDATE queued_jobs SET state='running' WHERE id=?", (self._job_id,))
                        other.close()
                    return self._conn.execute(sql, *args)
                def __getattr__(self, name):
                    return getattr(self._conn, name)
                def __enter__(self):
                    self._conn.__enter__(); return self
                def __exit__(self, *exc):
                    return self._conn.__exit__(*exc)

            async def scenario():
                _, _, job = await queue.enqueue("run", "x", msg, FakeOutbound(), runner)
                queue._get_conn = lambda: RacingConn(real_get_conn(), job.id)
                ok, _text = await queue.cancel(job.id)
                queue._get_conn = real_get_conn
                return ok, await queue.get_job(job.id)

            ok, job = asyncio.run(scenario())
            self.assertFalse(ok)
            self.assertEqual(job.state, QueueJobState.RUNNING)


if __name__ == "__main__":
    unittest.main()
