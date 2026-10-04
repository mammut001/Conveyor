"""Dispatch-level routing for read-only host questions.

Drives handlers.dispatch.dispatch, the same entry the bots use.
"""
from __future__ import annotations

import importlib
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

from channel.types import InboundMessage
from config import Settings
from handlers.dispatch import dispatch

_dispatch_mod = importlib.import_module("handlers.dispatch")
_runner_mod = importlib.import_module("handlers.tools.runner")
_jobs_mod = importlib.import_module("handlers.jobs")


@dataclass
class FakePort:
    replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = False
    supports_attachments: bool = False

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "ph"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.replies.append(text)
        return "new"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        return True

    async def reply_with_buttons(self, msg, text, buttons) -> str:
        self.replies.append(text)
        return "btn"


def _settings(tmp: Path, *, chat_tools: bool) -> Settings:
    mem = tmp / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (tmp / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp / "ws").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=12345,
        codex_workspace_root=tmp / "ws",
        codex_bin="codex",
        codex_task_root=tmp / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem,
        user_timezone="UTC",
        chat_mode="off",
        chat_tools_enabled=chat_tools,
    )


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m1",
        text=text,
        chat_type="p2p",
    )


class ReadonlyHostRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = AsyncMock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def _dispatch(self, text: str, *, chat_tools: bool):
        port = FakePort()
        settings = _settings(self.root, chat_tools=chat_tools)
        codex = AsyncMock()
        with patch.object(_dispatch_mod, "handle_codex_job", codex), patch.object(
            _runner_mod, "handle_codex_job", codex
        ), patch.object(_jobs_mod, "handle_codex_job", codex):
            await dispatch(_msg(text), port, settings, self.runner)
        return port, codex

    async def test_tools_on_machine_disk_question_does_not_start_codex(self) -> None:
        port, codex = await self._dispatch("我的服务器磁盘还剩多少", chat_tools=True)
        self.assertEqual(codex.await_count, 0)
        self.assertTrue(port.replies, "disk tool produced no reply")
        body = port.replies[-1]
        self.assertIn("磁盘使用快照", body)
        self.assertIn("%", body)

    async def test_tools_on_repo_edit_still_starts_codex(self) -> None:
        text = "修一下我的仓库里挂掉的测试"
        port, codex = await self._dispatch(text, chat_tools=True)
        self.assertGreaterEqual(codex.await_count, 1)
        prompt = codex.await_args.kwargs.get("prompt") or codex.await_args.args[-1]
        self.assertIn("修一下", prompt)
        self.assertFalse(any("磁盘使用快照" in reply for reply in port.replies))

    async def test_tools_off_machine_disk_question_starts_codex(self) -> None:
        text = "我的服务器磁盘还剩多少"
        _port, codex = await self._dispatch(text, chat_tools=False)
        self.assertGreaterEqual(codex.await_count, 1)
        prompt = codex.await_args.kwargs.get("prompt") or codex.await_args.args[-1]
        self.assertIn(text, prompt)

    async def test_direct_disk_phrase_still_skips_codex(self) -> None:
        port, codex = await self._dispatch("看看磁盘", chat_tools=True)
        self.assertEqual(codex.await_count, 0)
        self.assertTrue(port.replies)
        self.assertIn("磁盘使用快照", port.replies[-1])
        self.assertIn("%", port.replies[-1])

    async def test_tools_on_service_question_does_not_start_codex(self) -> None:
        port, codex = await self._dispatch("我的机器上哪些服务是否正常", chat_tools=True)
        self.assertEqual(codex.await_count, 0)
        self.assertTrue(port.replies)
        self.assertIn("服务状态", port.replies[-1])

    async def test_tools_on_edit_that_mentions_disk_still_starts_codex(self) -> None:
        text = "删掉我的服务器上的缓存，把磁盘腾出来"
        _port, codex = await self._dispatch(text, chat_tools=True)
        self.assertGreaterEqual(codex.await_count, 1)
