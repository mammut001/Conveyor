"""The desktop planner attaches a saved observe screenshot to Codex."""
from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from config import Settings
from desktop_computer_loop import run_computer_loop
from desktop_computer_planner import CodexPlanner, maybe_simple_digit_action
from desktop_screenshot import resolve_screenshot_dir


def _settings(root: Path) -> Settings:
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "ws").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=12345,
        codex_workspace_root=root / "ws",
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem,
        user_timezone="UTC",
        chat_mode="off",
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_always_direct=True,
    )


def _tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = b"\x00\xff\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class _Stdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Proc:
    stdin_seen = b""
    payload = '{"action":"wait","seconds":1}'

    def __init__(self, args: tuple) -> None:
        self.args = args
        self.stdin = _Stdin()
        self.returncode = 0
        out = args[args.index("--output-last-message") + 1]
        Path(out).write_text(self.payload, encoding="utf-8")

    async def wait(self) -> int:
        _Proc.stdin_seen += self.stdin.data
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class PlannerImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_call_receives_the_saved_screenshot(self) -> None:
        commands: list[tuple] = []
        _Proc.stdin_seen = b""
        _Proc.payload = '{"action":"wait","seconds":1}'

        async def fake_exec(*args, **kwargs):
            commands.append(args)
            return _Proc(args)

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            shot_id = "20261005T000000Z-cua-abc12345"
            shot_dir = resolve_screenshot_dir(settings)
            shot_dir.mkdir(parents=True, exist_ok=True)
            png_path = shot_dir / f"{shot_id}.png"
            png_path.write_bytes(_tiny_png())
            planner = CodexPlanner(settings)
            with mock.patch(
                "desktop_computer_planner.asyncio.create_subprocess_exec",
                fake_exec,
            ):
                action = await planner.next_action(
                    goal="打开计算器并点 1",
                    observation={
                        "screenshot_id": shot_id,
                        "width": 1,
                        "height": 1,
                    },
                    trajectory=[],
                    steps_used=1,
                    max_steps=8,
                )
        self.assertEqual(action.get("action"), "wait")
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("--image", command)
        image_at = command.index("--image")
        self.assertEqual(command[image_at + 1], str(png_path))
        self.assertLess(image_at, command.index("--json"))
        self.assertEqual(command[-1], "-")
        self.assertNotIn(b"\x89PNG", _Proc.stdin_seen)

    async def test_model_call_without_an_image_stays_text(self) -> None:
        commands: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            commands.append(args)
            return _Proc(args)

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            planner = CodexPlanner(settings)
            with mock.patch(
                "desktop_computer_planner.asyncio.create_subprocess_exec",
                fake_exec,
            ):
                action = await planner.next_action(
                    goal="打开计算器并点 1",
                    observation={"initial": True},
                    trajectory=[],
                    steps_used=0,
                    max_steps=8,
                )
        self.assertEqual(action.get("action"), "wait")
        self.assertNotIn("--image", commands[0])

    async def test_loop_asks_the_model_when_the_screenshot_has_no_buttons(self) -> None:
        commands: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            commands.append(args)
            return _Proc(args)

        class ShotBackend:
            async def execute_step(self, settings, task_id, step_id, action):
                if action.get("action") == "observe":
                    return {
                        "result_ok": True,
                        "action_type": "observe",
                        "screenshot_id": "20261005T000000Z-cua-abc12345",
                        "width": 1,
                        "height": 1,
                    }
                return {"result_ok": True, "action_type": action.get("action")}

        _Proc.payload = '{"action":"done","summary":"ok"}'
        goal = "打开计算器并点 1"
        bare = maybe_simple_digit_action(
            goal=goal,
            observation={"screenshot_id": "20261005T000000Z-cua-abc12345"},
            trajectory=[],
        )
        self.assertIsNone(bare)

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            shot_id = "20261005T000000Z-cua-abc12345"
            shot_dir = resolve_screenshot_dir(settings)
            shot_dir.mkdir(parents=True, exist_ok=True)
            png_path = shot_dir / f"{shot_id}.png"
            png_path.write_bytes(_tiny_png())
            planner = CodexPlanner(settings)
            with mock.patch(
                "desktop_computer_planner.asyncio.create_subprocess_exec",
                fake_exec,
            ):
                result = await run_computer_loop(
                    settings,
                    goal,
                    planner=planner,
                    backend=ShotBackend(),
                    max_steps=4,
                    max_seconds=30,
                    direct_mode=True,
                )
        self.assertEqual(result.get("status"), "done", result)
        self.assertTrue(commands)
        command = commands[0]
        self.assertIn("--image", command)
        self.assertEqual(command[command.index("--image") + 1], str(png_path))
        self.assertNotIn("\x89PNG", " ".join(str(part) for part in command))
