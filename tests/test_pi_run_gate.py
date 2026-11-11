"""/run and /fix reach Pi (no sandbox): destructive ones must ask first."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.dispatch import dispatch
from tests.test_computer_dispatch import FakePort, _msg, _settings


class PiRunGateTest(unittest.IsolatedAsyncioTestCase):
    async def _dispatch(self, text: str, runner_name: str):
        with tempfile.TemporaryDirectory() as raw:
            settings = _settings(Path(raw), always_direct=False)
            runner = SimpleNamespace(name=runner_name)
            confirm, job = AsyncMock(), AsyncMock()
            with patch("handlers.tools.runner._request_confirmation", confirm), \
                    patch("handlers.dispatch.handle_codex_job", job), \
                    patch("conveyor.core.job_lanes.runner_for_chat", lambda r, *a, **k: r):
                await dispatch(_msg(text), FakePort(), settings, runner)
            return confirm, job

    async def test_destructive_run_on_pi_asks_for_approval(self) -> None:
        for text in ("/run rm -rf /srv/data", "/fix git push --force origin main"):
            with self.subTest(text=text):
                confirm, job = await self._dispatch(text, "pi")
                self.assertEqual(confirm.await_count, 1)
                self.assertEqual(confirm.await_args.args[3], "pi.destructive_task")
                self.assertEqual(job.await_count, 0)

    async def test_ordinary_run_on_pi_runs(self) -> None:
        confirm, job = await self._dispatch("/run 跑一下测试", "pi")
        self.assertEqual(confirm.await_count, 0)
        self.assertEqual(job.await_count, 1)

    async def test_destructive_caption_with_attachment_on_pi_asks_for_approval(self) -> None:
        from dataclasses import replace
        from channel.types import Attachment
        with tempfile.TemporaryDirectory() as raw:
            settings = _settings(Path(raw), always_direct=False)
            msg = replace(_msg("照图里的目录执行 rm -rf /srv/data"),
                          attachments=(Attachment(kind="image", ref="f1"),))
            confirm, ctx = AsyncMock(), AsyncMock()
            with patch("handlers.tools.runner._request_confirmation", confirm), \
                    patch("handlers.dispatch.handle_context_job", ctx), \
                    patch("conveyor.core.job_lanes.runner_for_chat", lambda r, *a, **k: r):
                await dispatch(msg, FakePort(), settings, SimpleNamespace(name="pi"))
            self.assertEqual(confirm.await_count, 1)
            self.assertEqual(ctx.await_count, 0)

    async def test_codex_backend_keeps_its_sandboxed_path(self) -> None:
        confirm, job = await self._dispatch("/run rm -rf build", "codex")
        self.assertEqual(confirm.await_count, 0)
        self.assertEqual(job.await_count, 1)

class DeepGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_deep_on_a_destructive_last_request_asks_for_approval(self) -> None:
        from handlers import chat
        with tempfile.TemporaryDirectory() as raw:
            settings = _settings(Path(raw), always_direct=False)
            msg = _msg("/deep")
            chat.set_last(chat.chat_key(msg, settings),
                          chat.LastRequest(codex_prompt="把 /srv/data 删了", confirm=True), settings=settings)
            confirm, job = AsyncMock(), AsyncMock()
            with patch("handlers.tools.runner._request_confirmation", confirm), \
                    patch("handlers.jobs.handle_codex_job", job):
                await chat.handle_deep(msg, FakePort(), SimpleNamespace(name="pi", settings=settings),
                                       settings=settings)
            self.assertEqual(confirm.await_count, 1)
            self.assertEqual(job.await_count, 0)


if __name__ == "__main__":
    unittest.main()
