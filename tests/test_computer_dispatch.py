"""Dispatch-level gate for natural-language computer use.

Drives handlers.dispatch.dispatch. The desktop loop is stubbed at
run_computer_loop so the test still enters the real tool and its
preflight (blocked keywords and the direct-mode arm gate).
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
from handlers.intent import computer_chat_budget_seconds, computer_chat_route, route_intent

_loop_mod = importlib.import_module("desktop_computer_loop")

_SAFE = "在电脑上打开文件管理器"
_BLOCKED = "在电脑上打开文件管理器 password"
_ARM = "Direct 模式未启用"


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


def _settings(tmp: Path, *, always_direct: bool) -> Settings:
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
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_always_direct=always_direct,
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


class ComputerDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = AsyncMock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def _dispatch(self, text: str, *, always_direct: bool):
        port = FakePort()
        settings = _settings(self.root, always_direct=always_direct)
        loop = AsyncMock(return_value={
            "ok": True,
            "status": "done",
            "task_id": "probe",
            "steps_used": 1,
            "summary": "opened",
        })
        with patch.object(_loop_mod, "run_computer_loop", loop):
            await dispatch(_msg(text), port, settings, self.runner)
        return port, loop

    async def test_always_direct_runs_computer_tool(self) -> None:
        port, loop = await self._dispatch(_SAFE, always_direct=True)
        self.assertGreaterEqual(loop.await_count, 1)
        reply = "\n".join(port.replies)
        self.assertNotIn(_ARM, reply)
        self.assertNotIn("/computer_arm", reply)
        self.assertNotIn("确认执行", reply)

    async def test_always_direct_off_stops_at_arm_gate(self) -> None:
        port, loop = await self._dispatch(_SAFE, always_direct=False)
        self.assertEqual(loop.await_count, 0)
        reply = "\n".join(port.replies)
        self.assertIn(_ARM, reply)
        self.assertIn("/computer_arm", reply)

    def test_htop_snapshot_is_not_a_desktop_task(self) -> None:
        htop = route_intent("帮我运行 htop 看看我的vps")
        self.assertEqual(htop.tools, ("htop",))
        desktop = route_intent(_SAFE)
        self.assertEqual(desktop.tools, ("computer.task",))
        still_desktop = route_intent("帮我运行文件管理器")
        self.assertEqual(still_desktop.tools, ("computer.task",))

    def test_web_chat_routes_a_desktop_sentence(self) -> None:
        desktop = computer_chat_route("打开计算器并点 1")
        self.assertIsNotNone(desktop)
        assert desktop is not None
        self.assertEqual(desktop.tools, ("computer.task",))
        self.assertIsNone(computer_chat_route("帮我运行 htop 看看我的vps"))
        self.assertIsNone(computer_chat_route("Hi there"))
        stopped = computer_chat_route("停下")
        self.assertIsNotNone(stopped)
        assert stopped is not None
        self.assertEqual(stopped.tools, ("computer.stop",))
        self.assertEqual(computer_chat_budget_seconds(object(), "打开计算器"), 660.0)
        self.assertIsNone(computer_chat_budget_seconds(object(), "Hi there"))

    async def test_blocked_keyword_does_not_start_loop(self) -> None:
        port, loop = await self._dispatch(_BLOCKED, always_direct=True)
        self.assertEqual(loop.await_count, 0)
        reply = "\n".join(port.replies)
        self.assertIn("受限关键词", reply)
        self.assertNotIn("确认执行", reply)
