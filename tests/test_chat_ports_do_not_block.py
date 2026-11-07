"""Telegram and Feishu handle one update per chat at a time.

If the message handler waited for the agent job, /cancel, /status and card
buttons in that chat were only processed after the job had finished.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from channel.feishu import FeishuOutbound
from channel.telegram import TelegramOutbound
from channel.types import InboundMessage


class ChatPortsDoNotBlockTest(unittest.TestCase):
    def test_ports_return_once_the_job_is_queued(self) -> None:
        import handlers.jobs as jobs
        msg = InboundMessage(channel="telegram", operator_id="1", chat_id="1", message_id="m", text="/run x")
        for port in (TelegramOutbound(SimpleNamespace()), FeishuOutbound(SimpleNamespace())):
            with self.subTest(port=type(port).__name__):
                submit = AsyncMock(return_value=(True, "queued", None))
                with patch.object(jobs, "submit_codex_job", submit):
                    asyncio.run(jobs.handle_codex_job(msg, port, SimpleNamespace(), prompt="x"))
                self.assertIs(submit.await_args.kwargs["wait"], False)


if __name__ == "__main__":
    unittest.main()
