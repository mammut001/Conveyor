"""Regression coverage for Telegram/Feishu routed poller lifecycle."""
import asyncio
import unittest
from unittest.mock import patch

from telegram.ext import Application

from agent_result_poller_lifecycle import (
    AgentResultPollingApplication, cancel_poller, configure_telegram_poller,
    create_poller,
)


async def _fake_ptb_start(application):
    application._running = True


async def _fake_ptb_stop(application):
    application._running = False


class AgentResultPollerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.application = (
            Application.builder()
            .token("123456:TESTTOKEN")
            .application_class(AgentResultPollingApplication)
            .build()
        )

    async def test_telegram_starts_only_after_application_start_and_stops(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def poll():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        configure_telegram_poller(self.application, poll)
        self.assertNotIn("agent_result_poller", self.application.bot_data)
        with (patch.object(Application, "start", _fake_ptb_start),
              patch.object(Application, "stop", _fake_ptb_stop)):
            await self.application.start()
            task = self.application.bot_data["agent_result_poller"]
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(self.application.running)
            self.assertFalse(task.done())
            await self.application.stop()

        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("agent_result_poller", self.application.bot_data)

    async def test_crashed_telegram_poller_logs_traceback(self):
        async def crash():
            raise RuntimeError("test-poller-crash")

        configure_telegram_poller(self.application, crash)
        with (patch.object(Application, "start", _fake_ptb_start),
              patch.object(Application, "stop", _fake_ptb_stop),
              self.assertLogs("agent_result_poller_lifecycle", level="ERROR") as logs):
            await self.application.start()
            task = self.application.bot_data["agent_result_poller"]
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)  # Flush add_done_callback.
            await self.application.stop()

        self.assertTrue(any("test-poller-crash" in entry for entry in logs.output))

    async def test_feishu_style_poller_cancels_cleanly_and_logs_crashes(self):
        entered = asyncio.Event()

        async def poll():
            entered.set()
            await asyncio.Future()

        task = create_poller(poll, name="conveyor-agent-feishu-results")
        await asyncio.wait_for(entered.wait(), timeout=1)
        await cancel_poller(task)
        self.assertTrue(task.cancelled())

        async def crash():
            raise ValueError("feishu-poller-crash")

        with self.assertLogs("agent_result_poller_lifecycle", level="ERROR") as logs:
            task = create_poller(crash, name="conveyor-agent-feishu-results")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertTrue(any("feishu-poller-crash" in entry for entry in logs.output))


if __name__ == "__main__":
    unittest.main()
