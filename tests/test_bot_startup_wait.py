"""Startup Telegram reachability wait: retry network outages without crash-looping."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import telegram.error


def _load_bot():
    root = tempfile.mkdtemp()
    env = {
        "CONVEYOR_ENV_FILE": os.devnull,
        "TELEGRAM_BOT_TOKEN": "test",
        "TELEGRAM_ALLOWED_USER_ID": "1001",
        "CODEX_WORKSPACE_ROOT": os.path.join(root, "w"),
        "CODEX_TASK_ROOT": os.path.join(root, "t"),
        "CODEX_MEMORY_ROOT": os.path.join(root, "m"),
    }
    with patch.dict(os.environ, env):
        from conveyor.entrypoints import bot
    return bot


class BotStartupWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = _load_bot()

    def test_retries_transient_network_errors_and_logs_warnings_without_traceback(self) -> None:
        outcomes: list[BaseException | None] = [
            telegram.error.TimedOut(),
            telegram.error.TimedOut(),
            telegram.error.NetworkError("Bad Gateway"),
            None,
        ]
        sleeps: list[float] = []

        def fake_probe() -> None:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        with self.assertLogs(self.bot.logger, level="INFO") as cm:
            self.bot.wait_for_telegram("tok", sleep=sleeps.append, probe=fake_probe)

        self.assertEqual(outcomes, [])
        self.assertEqual(sleeps, [5, 10, 20])
        warnings = [r for r in cm.records if r.levelname == "WARNING"]
        infos = [r for r in cm.records if r.levelname == "INFO"]
        self.assertEqual(len(warnings), 3)
        self.assertEqual(len(infos), 1)
        self.assertIn("Telegram reachable again", infos[0].getMessage())
        for record in cm.records:
            self.assertIsNone(record.exc_info)

    def test_invalid_token_reraises_immediately_without_sleeping(self) -> None:
        sleeps: list[float] = []

        def fake_probe() -> None:
            raise telegram.error.InvalidToken()

        with self.assertRaises(telegram.error.InvalidToken):
            self.bot.wait_for_telegram("tok", sleep=sleeps.append, probe=fake_probe)

        self.assertEqual(sleeps, [])

    def test_forbidden_reraises_immediately_without_sleeping(self) -> None:
        sleeps: list[float] = []

        def fake_probe() -> None:
            raise telegram.error.Forbidden("Forbidden")

        with self.assertRaises(telegram.error.Forbidden):
            self.bot.wait_for_telegram("tok", sleep=sleeps.append, probe=fake_probe)

        self.assertEqual(sleeps, [])

    def test_delay_caps_at_max_delay(self) -> None:
        outcomes: list[BaseException | None] = [
            telegram.error.TimedOut() for _ in range(10)
        ] + [None]
        sleeps: list[float] = []

        def fake_probe() -> None:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        with self.assertLogs(self.bot.logger, level="INFO"):
            self.bot.wait_for_telegram(
                "tok",
                sleep=sleeps.append,
                probe=fake_probe,
                max_delay=60,
            )

        self.assertEqual(sleeps, [5, 10, 20, 40, 60, 60, 60, 60, 60, 60])

    def test_unrelated_exception_propagates(self) -> None:
        sleeps: list[float] = []

        def fake_probe() -> None:
            raise ValueError("unexpected failure")

        with self.assertRaises(ValueError):
            self.bot.wait_for_telegram("tok", sleep=sleeps.append, probe=fake_probe)

        self.assertEqual(sleeps, [])

    def test_default_probe_leaves_an_event_loop_for_run_polling(self) -> None:
        # run_polling() calls asyncio.get_event_loop(); asyncio.run() in the probe
        # would leave none and crash every start.
        import asyncio

        calls: list[str] = []

        async def fake_get_me(token: str) -> None:
            calls.append(token)

        policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        try:
            with patch.object(self.bot, "_probe_get_me", fake_get_me):
                self.bot.wait_for_telegram("tok", sleep=lambda _d: None)
            loop = asyncio.get_event_loop()
            self.assertFalse(loop.is_closed())
            loop.close()
        finally:
            asyncio.set_event_loop_policy(policy)
        self.assertEqual(calls, ["tok"])


if __name__ == "__main__":
    unittest.main()
