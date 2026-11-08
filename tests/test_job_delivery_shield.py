"""Cancelling the caller that waits for a job must not drop the job's answer."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from channel.types import InboundMessage
from handlers.job_queue import reset_job_queue
from handlers.jobs import handle_codex_job
from runner.types import JobMode


class Port:
    def __init__(self):
        self.sent = []

    async def reply(self, msg, text):
        self.sent.append(text)
        return "ph"

    async def send_new(self, msg, text):
        self.sent.append(text)
        return "n"

    async def edit_progress(self, msg, pid, text):
        self.sent.append(text)
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        return "b"


class JobDeliveryShieldTest(unittest.TestCase):
    def test_answer_is_delivered_after_the_waiter_is_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.dict(os.environ, {"CODEX_MEMORY_ROOT": raw}):
                reset_job_queue()
                job = SimpleNamespace(id="j1", state="running", summary=None, error=None)

                async def fake_start(mode, prompt, progress):
                    async def finish():
                        await asyncio.sleep(0.4)
                        job.summary, job.state = "最终答案 42", "completed"
                    asyncio.get_running_loop().create_task(finish())
                    return job

                runner = mock.Mock()
                runner.start = fake_start
                runner.current_job = None
                runner.settings = SimpleNamespace(
                    codex_memory_root=Path(raw), conveyor_progress_mode="verbose",
                    conveyor_session_enabled=False, conveyor_max_jobs_per_hour=60,
                    conveyor_max_pending_jobs=20)
                msg = InboundMessage(channel="web", operator_id="o", chat_id="c", message_id="m", text="x")
                port = Port()

                async def scenario():
                    waiter = asyncio.create_task(
                        handle_codex_job(msg, port, runner, mode=JobMode.RUN, prompt="x", wait=True))
                    await asyncio.sleep(0.1)
                    waiter.cancel()          # web turn timed out / client went away
                    await asyncio.sleep(1.5)

                with mock.patch("handlers.jobs._sleep", new=lambda s: asyncio.sleep(0.05)):
                    asyncio.run(scenario())
                reset_job_queue()
        self.assertIn("最终答案 42", port.sent)


if __name__ == "__main__":
    unittest.main()
