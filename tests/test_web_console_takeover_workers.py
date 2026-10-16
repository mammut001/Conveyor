"""Production takeover Web entrypoint must own the same agent workers as web_console."""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import web_console_takeover


class TakeoverAgentWorkersTests(unittest.TestCase):
    def _run_entrypoint(self, *, enabled: bool):
        started = []
        stopped = []
        loops = []

        async def delegation_poll(_worker):
            started.append("delegation")
            try:
                await asyncio.Future()
            finally:
                stopped.append("delegation")

        async def retention_poll(_settings):
            started.append("retention")
            try:
                await asyncio.Future()
            finally:
                stopped.append("retention")

        original_new_loop = asyncio.new_event_loop

        def new_loop():
            loop = original_new_loop()
            loops.append(loop)
            # Both scheduled workers run, then the simulated main loop exits.
            loop.call_later(0.02, loop.stop)
            return loop

        settings = SimpleNamespace(
            conveyor_takeover_enabled=False,
            conveyor_web_host="127.0.0.1",
            conveyor_web_port=12345,
            conveyor_web_token="test",
            memory_v2_enabled=False,
        )
        flags = {
            "CONVEYOR_AGENT_MAILBOX": "1" if enabled else "0",
            "CONVEYOR_AGENT_DELEGATION": "1" if enabled else "0",
        }
        try:
            with (
                mock.patch.dict(os.environ, flags),
                mock.patch("sys.argv", ["web_console_takeover.py"]),
                mock.patch("web_console_takeover.load_runtime_settings", return_value=settings),
                mock.patch("web_console_takeover.validate_web_config"),
                mock.patch("web_console_takeover.configure_logging"),
                mock.patch("web_console_takeover.CodexRunner"),
                mock.patch("web_console_takeover.get_job_queue"),
                mock.patch("web_console_takeover.WebControl"),
                mock.patch("web_console_takeover.WebTakeover"),
                mock.patch("web_console_takeover.TakeoverWebConsoleServer") as server_factory,
                mock.patch("web_console_takeover.threading.Thread"),
                mock.patch("web_console_takeover.start_extra_listeners"),
                mock.patch("web_console_takeover.asyncio.new_event_loop", side_effect=new_loop),
                mock.patch("routines.start_routines_worker", return_value=None),
                mock.patch("approval_relay.start_relay_worker", return_value=None),
                mock.patch("agent_context.enabled", return_value=enabled),
                mock.patch("agent_mailbox.enabled", return_value=enabled),
                mock.patch("agent_delegation_worker.Worker.run", new=delegation_poll),
                mock.patch("agent_security.retention_worker", new=retention_poll),
            ):
                web_console_takeover.main()
            server_factory.return_value.shutdown.assert_called_once()
            server_factory.return_value.server_close.assert_called_once()
        finally:
            asyncio.set_event_loop(None)
        self.assertEqual(len(loops), 1)
        self.assertTrue(loops[0].is_closed())
        return started, stopped

    def test_flags_on_starts_and_cancels_both_workers(self):
        started, stopped = self._run_entrypoint(enabled=True)
        self.assertCountEqual(started, ["delegation", "retention"])
        self.assertCountEqual(stopped, ["delegation", "retention"])

    def test_flags_off_does_not_schedule_workers(self):
        started, stopped = self._run_entrypoint(enabled=False)
        self.assertEqual(started, [])
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
