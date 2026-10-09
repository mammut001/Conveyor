"""The desktop planner attaches a saved observe screenshot to Codex."""
from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from config import Settings
from desktop_computer_loop import run_computer_loop
from desktop_computer_planner import (
    CodexPlanner,
    followup_click_labels,
    maybe_followup_label_action,
    maybe_simple_digit_action,
)
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


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, _n: int) -> bytes:
        data, self._data = self._data, b""
        return data


class _Proc:
    stdin_seen = b""
    payload = '{"action":"wait","seconds":1}'
    plan: list[dict] = []

    def __init__(self, args: tuple) -> None:
        spec = _Proc.plan.pop(0) if _Proc.plan else {}
        self.args = args
        self.stdin = _Stdin()
        self.returncode = spec.get("code", 0)
        self.stdout = _Bytes(spec.get("stdout", b""))
        self.stderr = _Bytes(b"")
        out = args[args.index("--output-last-message") + 1]
        Path(out).write_text(spec.get("payload", self.payload), encoding="utf-8")

    async def wait(self) -> int:
        _Proc.stdin_seen += self.stdin.data
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class PlannerImageTest(unittest.IsolatedAsyncioTestCase):
    async def test_fullscreen_decisions_use_only_the_current_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            planner = CodexPlanner(_settings(Path(temp)), screen_coordinates=True,
                                   resume_thread_id="11111111-1111-1111-1111-111111111111")
            with mock.patch.object(planner, "_run_codex", new=mock.AsyncMock(
                    return_value='{"action":"hotkey","keys":["enter"]}')) as run:
                action = await planner.next_action(
                    goal="Open the webpage", observation={"browser_navigation_status": "awaiting_submit"},
                    trajectory=[], steps_used=4, max_steps=16,
                )
            self.assertEqual(action["keys"], ["enter"])
            self.assertIs(run.await_args.kwargs["resume"], False)
            self.assertIn("browser_navigation_status=awaiting_submit", run.await_args.args[0])

    async def test_malformed_model_action_gets_one_safe_retry(self):
        for replies, expected in (
            (["{broken}", '{"action":"observe"}'], "observe"),
            (["{broken}", "{still broken}"], "stop"),
        ):
            with tempfile.TemporaryDirectory() as temp:
                planner = CodexPlanner(_settings(Path(temp)))
                with mock.patch.object(planner, "_run_codex", new=mock.AsyncMock(side_effect=replies)) as run:
                    action = await planner.next_action(
                        goal="Read the browser", observation={}, trajectory=[],
                        steps_used=0, max_steps=16,
                    )
                self.assertEqual(action["action"], expected)
                self.assertEqual(run.await_count, 2)
                self.assertIs(run.await_args.kwargs["resume"], False)

    async def test_model_call_receives_the_saved_screenshot(self) -> None:
        commands: list[tuple] = []
        _Proc.stdin_seen = b""
        _Proc.plan = []
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
                    png = png_path.read_bytes()
                    return {
                        "result_ok": True,
                        "action_type": "observe",
                        "screenshot_id": "20261005T000000Z-cua-abc12345",
                        "sha256": hashlib.sha256(png).hexdigest(),
                        "width": 1,
                        "height": 1,
                    }
                return {"result_ok": True, "action_type": action.get("action")}

        _Proc.plan = []
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

    async def test_later_step_resumes_the_same_session(self) -> None:
        commands: list[tuple] = []
        thread_id = "11111111-1111-4111-8111-111111111111"
        _Proc.plan = [
            {
                "stdout": (
                    '{"type":"thread.started","thread_id":"%s"}\n' % thread_id
                ).encode(),
                "payload": '{"action":"observe"}',
            },
            {"payload": '{"action":"done","summary":"ok"}'},
        ]
        _Proc.stdin_seen = b""

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
            observation = {"screenshot_id": shot_id, "width": 1, "height": 1}
            with mock.patch(
                "desktop_computer_planner.asyncio.create_subprocess_exec",
                fake_exec,
            ):
                first = await planner.next_action(
                    goal="打开计算器并点等号",
                    observation={"initial": True},
                    trajectory=[],
                    steps_used=0,
                    max_steps=8,
                )
                second = await planner.next_action(
                    goal="打开计算器并点等号",
                    observation=observation,
                    trajectory=[{"action_type": "observe", "result_ok": True}],
                    steps_used=1,
                    max_steps=8,
                )
        self.assertEqual(first.get("action"), "observe")
        self.assertEqual(second.get("action"), "done")
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][1], "exec")
        self.assertNotIn("resume", commands[0])
        resumed = commands[1]
        self.assertEqual(resumed[:3], (settings.codex_bin, "exec", "resume"))
        self.assertIn("--image", resumed)
        image_at = resumed.index("--image")
        self.assertEqual(resumed[image_at + 1], str(png_path))
        self.assertLess(image_at, resumed.index("--json"))
        self.assertNotIn("--last", resumed)
        self.assertEqual(resumed[-2], thread_id)
        self.assertEqual(resumed[-1], "-")
        self.assertNotIn(b"\x89PNG", _Proc.stdin_seen)

    async def test_failed_resume_starts_a_fresh_session(self) -> None:
        commands: list[tuple] = []
        thread_id = "22222222-2222-4222-8222-222222222222"
        _Proc.plan = [
            {
                "stdout": (
                    '{"type":"thread.started","thread_id":"%s"}\n' % thread_id
                ).encode(),
                "payload": '{"action":"observe"}',
            },
            {"code": 1, "payload": ""},
            {"payload": '{"action":"wait","seconds":1}'},
        ]

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
                await planner.next_action(
                    goal="打开计算器并点等号",
                    observation={"initial": True},
                    trajectory=[],
                    steps_used=0,
                    max_steps=8,
                )
                action = await planner.next_action(
                    goal="打开计算器并点等号",
                    observation={"initial": True},
                    trajectory=[{"action_type": "observe", "result_ok": True}],
                    steps_used=1,
                    max_steps=8,
                )
        self.assertEqual(action.get("action"), "wait")
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[1][2], "resume")
        self.assertEqual(commands[2][1], "exec")
        self.assertNotIn("resume", commands[2])
        self.assertNotIn("--last", commands[1])
        self.assertNotIn("--last", commands[2])

    def test_named_followup_presses_the_button_without_clearing(self) -> None:
        self.assertIsNone(followup_click_labels("再点1"))
        self.assertIsNone(followup_click_labels("再输入abc"))
        self.assertIsNone(followup_click_labels("打开计算器"))
        self.assertIsNone(followup_click_labels("再点password"))
        self.assertEqual(
            followup_click_labels("然后再点等号"),
            ("等号", "=", "equals"),
        )
        self.assertEqual(followup_click_labels("点击等号。")[0], "等号")

        hints = {
            "pid": 42,
            "window_id": 7,
            "element_hints": [
                {"label": "Clear", "element_index": 1},
                {"label": "=", "element_index": 4},
            ],
        }
        click = maybe_followup_label_action(
            goal="再点等号",
            observation=hints,
            trajectory=[],
        )
        self.assertIsNotNone(click)
        assert click is not None
        self.assertEqual(click["action"], "click")
        self.assertEqual(click["element_index"], 4)
        self.assertEqual(click["_target_label"], "=")

        clear = maybe_followup_label_action(
            goal="再点清除",
            observation=hints,
            trajectory=[],
        )
        self.assertIsNotNone(clear)
        assert clear is not None
        self.assertEqual(clear["element_index"], 1)

        done = maybe_followup_label_action(
            goal="再点等号",
            observation=hints,
            trajectory=[{
                "action_type": "click",
                "result_ok": True,
                "clicked_label": "=",
            }],
        )
        self.assertEqual(done["action"], "done")
        self.assertIsNone(maybe_followup_label_action(
            goal="再点等号",
            observation={"screenshot_id": "shot-chat"},
            trajectory=[],
        ))
        self.assertEqual(
            maybe_followup_label_action(
                goal="再点等号",
                observation={"initial": True},
                trajectory=[],
            ),
            {"action": "observe"},
        )

        chat = {
            "pid": 9,
            "window_id": 3,
            "screenshot_id": "shot-chat",
            "element_hints": [{"label": "发送", "element_index": 2}],
            "windows": [
                {
                    "app": "Desktop_chat_window.py",
                    "title": "Conveyor",
                    "pid": 9,
                    "window_id": 3,
                },
            ],
        }
        self.assertIsNone(maybe_followup_label_action(
            goal="再点等号",
            observation=chat,
            trajectory=[],
        ))
        chat["windows"] = [
            {
                "app": "Desktop_chat_window.py",
                "title": "Conveyor",
                "pid": 9,
                "window_id": 3,
            },
            {
                "app": "gnome-calculator",
                "title": "Calculator",
                "pid": 42,
                "window_id": 7,
            },
        ]
        retarget = maybe_followup_label_action(
            goal="再点等号",
            observation=chat,
            trajectory=[],
        )
        self.assertEqual(retarget, {"action": "observe", "pid": 42, "window_id": 7})
        self.assertNotIn("target_app", retarget or {})

        titled = dict(chat)
        titled["windows"] = [{
            "app": "org.gnome.Calculator",
            "title": "计算器",
            "pid": 42,
            "window_id": 7,
        }]
        titled_action = maybe_followup_label_action(
            goal="再点等于",
            observation=titled,
            trajectory=[],
        )
        self.assertEqual((titled_action or {}).get("pid"), 42)

        already = {
            "pid": 42,
            "window_id": 7,
            "screenshot_id": "shot-calc",
            "element_hints": [{"label": "Clear", "element_index": 1}],
            "windows": [{
                "app": "gnome-calculator",
                "title": "Calculator",
                "pid": 42,
                "window_id": 7,
            }],
        }
        self.assertIsNone(maybe_followup_label_action(
            goal="再点等号",
            observation=already,
            trajectory=[],
        ))
