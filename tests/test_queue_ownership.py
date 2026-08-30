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


if __name__ == "__main__":
    unittest.main()
