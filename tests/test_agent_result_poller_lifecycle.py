"""Regression coverage for Telegram/Feishu routed poller lifecycle."""
import asyncio
import unittest
from unittest.mock import patch

from telegram.ext import Application
import telegram.error

from agent_coordination.agent_result_poller_lifecycle import (
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


class AgentResultPollingApplicationRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.application = (
            Application.builder()
            .token("123456:TESTTOKEN")
            .application_class(AgentResultPollingApplication)
            .build()
        )
        self.delays = []

        async def fake_sleep(delay):
            self.delays.append(delay)

        self.application._sleep = fake_sleep

    async def test_initialize_retries_on_network_error(self):
        calls = 0
        async def fake_initialize(_app):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise telegram.error.TimedOut()

        with patch.object(Application, "initialize", fake_initialize):
            await self.application.initialize()

        self.assertEqual(calls, 3)
        self.assertEqual(self.delays, [5, 10])

    async def test_initialize_propagates_invalid_token(self):
        calls = 0
        async def fake_initialize(_app):
            nonlocal calls
            calls += 1
            raise telegram.error.InvalidToken()

        with patch.object(Application, "initialize", fake_initialize):
            with self.assertRaises(telegram.error.InvalidToken):
                await self.application.initialize()

        self.assertEqual(calls, 1)
        self.assertEqual(self.delays, [])

    async def test_initialize_caps_delay_at_300(self):
        calls = 0
        async def fake_initialize(_app):
            nonlocal calls
            calls += 1
            if calls <= 8:
                raise telegram.error.TimedOut()

        with patch.object(Application, "initialize", fake_initialize):
            await self.application.initialize()

        self.assertEqual(calls, 9)
        self.assertEqual(self.delays, [5, 10, 20, 40, 80, 160, 300, 300])

    async def test_default_sleep_is_called_with_the_delay_only(self):
        app = (
            Application.builder()
            .token("123456:TESTTOKEN")
            .application_class(AgentResultPollingApplication)
            .build()
        )
        calls = 0

        async def fake_initialize(_app):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise telegram.error.TimedOut()

        slept = []

        async def fake_asyncio_sleep(delay, *args):
            slept.append((delay, *args))

        with (patch.object(Application, "initialize", fake_initialize),
              patch("asyncio.sleep", fake_asyncio_sleep)):
            await app.initialize()
        self.assertEqual(slept, [(5,)])


if __name__ == "__main__":
    unittest.main()
