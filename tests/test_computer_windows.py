"""Window list for the desktop planner, and a failed step that continues."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from desktop_computer_loop import (
    FakeComputerBackend,
    HttpComputerBackend,
    run_computer_loop,
)
from desktop_computer_planner import CodexPlanner, ScriptedPlanner
from desktop_computer_requests import (
    claim_computer_step,
    create_computer_step,
    create_computer_task,
    fail_computer_step,
    get_computer_task,
    validate_computer_result,
)
from desktop_cua import LocalCuaTransport, _token_under_point, _window_local_point, summarize_windows


def _settings(root: Path, **over: Any) -> Settings:
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "ws").mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = dict(
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
    kwargs.update(over)
    return Settings(**kwargs)


class WindowListTest(unittest.TestCase):
    def test_front_app_window_ranks_above_panels(self) -> None:
        rows = summarize_windows([
            {"app_name": "Xfce4-panel", "title": "xfce4-panel", "z_index": 9,
             "pid": 1, "window_id": 10, "bounds": {"x": 0, "y": 0, "width": 100, "height": 30}},
            {"app_name": "Thunar", "title": "ubuntu", "z_index": 4,
             "pid": 4, "window_id": 40, "bounds": {"x": 10, "y": 20, "width": 400, "height": 300}},
            {"app_name": "Xfce4-terminal", "title": "Neutral Test Window", "z_index": 6,
             "pid": 6, "window_id": 60, "bounds": {"x": 30, "y": 40, "width": 500, "height": 300}},
            {"app_name": "Xfdesktop", "title": "Desktop", "z_index": 0,
             "pid": 2, "window_id": 20, "bounds": {"x": 0, "y": 0, "width": 1728, "height": 1084}},
        ])
        self.assertEqual([row["app"] for row in rows[:2]], ["Xfce4-terminal", "Thunar"])
        self.assertEqual(rows[0]["pid"], 6)
        self.assertEqual(rows[0]["window_id"], 60)
        self.assertLess(rows[-1]["z"], rows[0]["z"])

    def test_validate_keeps_short_window_list(self) -> None:
        cleaned = validate_computer_result({
            "result_ok": True,
            "action_type": "observe",
            "windows": [{
                "app": "Thunar",
                "title": "ubuntu",
                "z": 4,
                "pid": 4,
                "window_id": 40,
                "x": 10,
                "y": 20,
                "w": 400,
                "h": 300,
                "secret": "nope",
            }],
        })
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        window = cleaned["windows"][0]
        self.assertEqual(window["app"], "Thunar")
        self.assertEqual(window["pid"], 4)
        self.assertNotIn("secret", window)

    def test_collect_hints_prefers_front_window_and_lists_all(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)

        def fake_call(name, args=None, timeout=None):
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [
                    {"app_name": "Xfce4-panel", "title": "bar", "z_index": 9,
                     "pid": 1, "window_id": 10, "is_on_screen": True,
                     "bounds": {"x": 0, "y": 0, "width": 1728, "height": 30}},
                    {"app_name": "Thunar", "title": "ubuntu", "z_index": 4,
                     "pid": 4, "window_id": 40, "is_on_screen": True,
                     "bounds": {"x": 0, "y": 40, "width": 600, "height": 400}},
                    {"app_name": "Xfce4-terminal", "title": "shell", "z_index": 6,
                     "pid": 6, "window_id": 60, "is_on_screen": True,
                     "bounds": {"x": 0, "y": 40, "width": 700, "height": 500}},
                ]}}
            if name == "get_window_state":
                return {"ok": True, "data": {"elements": [
                    {"element_index": 1, "role": "AXButton", "label": "ok"},
                ]}}
            return {"ok": False}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        hints = transport._collect_ax_hints({})
        self.assertEqual(hints["pid"], 6)
        self.assertEqual(hints["window_id"], 60)
        self.assertEqual(hints["windows"][0]["app"], "Xfce4-terminal")
        self.assertIn("Thunar", [row["app"] for row in hints["windows"]])

    def test_desktop_center_click_raises_the_window(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "Thunar",
                    "title": "ubuntu",
                    "pid": 1532684,
                    "window_id": 25167357,
                    "bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
                }]}}
            if name == "bring_to_front":
                return {"ok": True, "data": {"window_id": 25167357}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 1532684, "window_id": 25167357, "x": 864, "y": 567},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["click_method"], "bring_to_front")
        self.assertEqual(
            [name for name, _args in calls],
            ["list_windows", "bring_to_front"],
        )
        self.assertEqual(calls[1][1]["pid"], 1532684)
        self.assertEqual(calls[1][1]["window_id"], 25167357)

    def test_near_origin_center_click_raises_the_window(self) -> None:
        # Neutral Test Window on the VPS: x=5, y=56, w=817, h=483.
        # Screen center (413.5, 297.5) also lies inside the local box.
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[str] = []

        def fake_call(name, args=None, timeout=None):
            calls.append(name)
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "Xfce4-terminal",
                    "title": "Neutral Test Window",
                    "pid": 1723842,
                    "window_id": 48234499,
                    "bounds": {"x": 5, "y": 56, "width": 817, "height": 483},
                }]}}
            if name == "bring_to_front":
                return {"ok": True, "data": {"window_id": 48234499}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {
                "action": "click",
                "pid": 1723842,
                "window_id": 48234499,
                "x": 413.5,
                "y": 297.5,
            },
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["click_method"], "bring_to_front")
        self.assertEqual(calls, ["list_windows", "bring_to_front"])
        self.assertNotIn("click", calls)

    def test_window_local_click_is_not_a_raise(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "Thunar",
                    "pid": 4,
                    "window_id": 40,
                    "bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
                }]}}
            if name == "get_window_state":
                return {"ok": True, "data": {
                    "capture_id": "capture_window_local",
                    "window_bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
                    "screenshot_width": 640,
                    "screenshot_height": 480,
                }}
            if name == "click":
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 4, "window_id": 40, "x": 320, "y": 240},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["click_method"], "xy_click")
        names = [name for name, _args in calls]
        self.assertNotIn("bring_to_front", names)
        self.assertEqual(names, ["list_windows", "get_window_state", "click"])
        click_args = calls[2][1] or {}
        self.assertEqual(click_args["x"], 320)
        self.assertEqual(click_args["y"], 240)
        self.assertEqual(click_args["capture_id"], "capture_window_local")
        self.assertEqual(click_args["session"], (calls[1][1] or {})["session"])
        self.assertNotIn("coordinate_frame", click_args)
        self.assertNotIn("element_index", click_args)

    def test_screen_point_inside_a_window_uses_the_screenshot(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "Calculator",
                    "pid": 4,
                    "window_id": 40,
                    "bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
                }]}}
            if name == "get_window_state":
                return {"ok": True, "data": {
                    "capture_id": "capture_screen_point",
                    "window_bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
                }}
            if name == "click":
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 4, "window_id": 40, "x": 700, "y": 400},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["click_method"], "xy_click")
        click_args = calls[-1][1] or {}
        self.assertEqual(calls[-1][0], "click")
        self.assertEqual(click_args["x"], 156)
        self.assertEqual(click_args["y"], 73)
        self.assertEqual(click_args["capture_id"], "capture_screen_point")
        self.assertNotIn("coordinate_frame", click_args)

    def test_pixel_on_a_button_presses_that_element(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "Calculator",
                    "pid": 4,
                    "window_id": 40,
                    "bounds": {"x": 658, "y": 301, "width": 412, "height": 526},
                }]}}
            if name == "get_window_state":
                return {"ok": True, "data": {
                    "capture_id": "capture_button",
                    "window_bounds": {"x": 658, "y": 301, "width": 412, "height": 526},
                    "elements": [
                        {
                            "role": "panel",
                            "element_token": "s0000001b:1",
                            "actions": ["click"],
                            "screenshot_frame": {"x": 0, "y": 0, "w": 412, "h": 526},
                        },
                        {
                            "role": "push button",
                            "label": "1",
                            "element_token": "s0000001b:23",
                            "actions": ["click"],
                            "screenshot_frame": {"x": 38, "y": 393, "w": 64, "h": 44},
                        },
                        {
                            "role": "edit bar",
                            "element_token": "s0000001b:34",
                            "screenshot_frame": {"x": 26, "y": 174, "w": 360, "h": 41},
                        },
                    ],
                }}
            if name == "click":
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 4, "window_id": 40, "x": 70, "y": 415},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["pixel_hit"], "element")
        click_args = calls[-1][1] or {}
        self.assertEqual(click_args["element_token"], "s0000001b:23")
        self.assertEqual(click_args["session"], (calls[1][1] or {})["session"])
        self.assertNotIn("x", click_args)
        self.assertNotIn("element_index", click_args)
        state_args = calls[1][1] or {}
        self.assertIs(state_args.get("include_accessibility_tree"), True)

    def test_pixel_on_non_accessible_canvas_uses_capture_id_and_raw_pixels(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "CanvasApp",
                    "pid": 42,
                    "window_id": 105,
                    "bounds": {"x": 100, "y": 100, "width": 400, "height": 400},
                }]}}
            if name == "get_window_state":
                return {"ok": True, "data": {
                    "capture_id": "capture_canvas_123",
                    "window_bounds": {"x": 100, "y": 100, "width": 400, "height": 400},
                    "elements": [],
                }}
            if name == "click":
                return {"ok": True, "data": {"summary": "Clicked at (150, 150)"}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 42, "window_id": 105, "x": 150, "y": 150},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["pixel_hit"], "capture")
        self.assertEqual(result["click_method"], "xy_click")
        self.assertEqual([name for name, _args in calls], ["list_windows", "get_window_state", "click"])
        click_args = calls[-1][1] or {}
        self.assertEqual(click_args["pid"], 42)
        self.assertEqual(click_args["window_id"], 105)
        self.assertEqual(click_args["x"], 150)
        self.assertEqual(click_args["y"], 150)
        self.assertEqual(click_args["capture_id"], "capture_canvas_123")
        self.assertEqual(click_args["session"], (calls[1][1] or {})["session"])
        self.assertNotIn("element_token", click_args)

    def test_pixel_click_resolves_window_id_from_pid_when_missing(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "list_windows":
                return {"ok": True, "data": {"windows": [{
                    "app_name": "CanvasApp",
                    "pid": 55,
                    "window_id": 999,
                    "bounds": {"x": 50, "y": 50, "width": 300, "height": 300},
                }]}}
            if name == "get_window_state":
                return {"ok": True, "data": {
                    "capture_id": "capture_canvas_999",
                    "window_bounds": {"x": 50, "y": 50, "width": 300, "height": 300},
                    "elements": [],
                }}
            if name == "click":
                return {"ok": True, "data": {"summary": "Clicked at (100, 100)"}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {"action": "click", "pid": 55, "x": 100, "y": 100},
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["pixel_hit"], "capture")
        self.assertEqual([name for name, _args in calls], ["list_windows", "get_window_state", "click"])
        click_args = calls[-1][1] or {}
        self.assertEqual(click_args["pid"], 55)
        self.assertEqual(click_args["window_id"], 999)
        self.assertEqual(click_args["capture_id"], "capture_canvas_999")

    def test_smallest_clickable_frame_wins(self) -> None:
        elements = [
            {
                "role": "panel",
                "element_token": "s0000001b:1",
                "actions": ["click"],
                "screenshot_frame": {"x": 0, "y": 0, "w": 400, "h": 500},
            },
            {
                "role": "push button",
                "element_token": "s0000001b:23",
                "screenshot_frame": {"x": 38, "y": 393, "w": 64, "h": 44},
            },
            {
                "role": "label",
                "element_token": "not-a-token",
                "actions": ["click"],
                "screenshot_frame": {"x": 38, "y": 393, "w": 10, "h": 10},
            },
        ]
        self.assertEqual(_token_under_point(elements, 70, 415), "s0000001b:23")
        self.assertEqual(_token_under_point(elements, 10, 10), "s0000001b:1")
        self.assertIsNone(_token_under_point(elements, 500, 500))

    def test_window_local_point_keeps_screenshot_pixels(self) -> None:
        state = {"window_bounds": {"x": 10, "y": 40, "width": 400, "height": 300}}
        self.assertEqual(_window_local_point(50, 60, state), (50, 60))
        self.assertEqual(_window_local_point(700, 400, {
            "window_bounds": {"x": 544, "y": 327, "width": 640, "height": 480},
        }), (156, 73))

    def test_missing_app_is_launched_before_observe(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[str] = []

        def fake_call(name, args=None, timeout=None):
            calls.append(name)
            if name == "list_apps":
                return {"ok": True, "data": {"apps": [{
                    "name": "Calculator",
                    "running": False,
                    "launch_path": "/usr/bin/gnome-calculator",
                }]}}
            if name == "launch_app":
                self.assertEqual(args.get("launch_path"), "/usr/bin/gnome-calculator")
                return {"ok": True, "data": {"pid": 99}}
            if name == "bring_to_front":
                self.assertEqual(args.get("pid"), 99)
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        with patch("desktop_cua.sys.platform", "darwin"):
            error = transport._prepare_target_app({"action": "observe", "target_app": "Calculator"})
        self.assertIsNone(error)
        self.assertEqual(calls, ["list_apps", "launch_app", "bring_to_front"])

    def test_labeled_button_click_uses_a_fresh_token(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)
        calls: list[tuple[str, dict | None]] = []

        def fake_call(name, args=None, timeout=None):
            calls.append((name, args))
            if name == "get_window_state":
                return {"ok": True, "data": {"elements": [{
                    "element_index": 23,
                    "role": "push button",
                    "label": "1",
                    "element_token": "s0000001a:23",
                }]}}
            if name == "click":
                return {"ok": True, "data": {"summary": "Clicked element [23]"}}
            return {"ok": False, "error": "unexpected_tool"}

        transport._call_tool = fake_call  # type: ignore[method-assign]
        result = transport._click(
            {
                "action": "click",
                "pid": 1837219,
                "window_id": 54525960,
                "element_index": 23,
                "element_token": "s00000001:23",
                "_target_label": "1",
            },
            "node",
        )
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["click_method"], "ax_click")
        self.assertEqual([name for name, _args in calls], ["get_window_state", "click"])
        state_args = calls[0][1] or {}
        click_args = calls[1][1] or {}
        self.assertEqual(state_args["pid"], 1837219)
        self.assertEqual(state_args["window_id"], 54525960)
        self.assertEqual(click_args["pid"], 1837219)
        self.assertEqual(click_args["element_token"], "s0000001a:23")
        self.assertEqual(click_args["session"], state_args["session"])
        self.assertNotIn("element_index", click_args)
        self.assertNotEqual(click_args["element_token"], "s00000001:23")

    def test_driver_exit_reports_stdout_code(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)

        class Proc:
            returncode = 1
            stdout = '{"code": "screenshot_context_missing", "pid": 1, "window_id": 2}'
            stderr = ""

        with patch("desktop_cua.subprocess.run", return_value=Proc()):
            result = transport._call_tool("click", {"x": 1, "y": 2})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "driver_exit_1:screenshot_context_missing")

    def test_driver_exit_reports_nested_refusal_code(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()))
        transport = LocalCuaTransport("cua-driver call", settings=settings)

        class Proc:
            returncode = 1
            stdout = '{"refusal": {"code": "invalid_arguments", "message": "click: unknown argument element_index"}, "status": "refused"}'
            stderr = ""

        with patch("desktop_cua.subprocess.run", return_value=Proc()):
            result = transport._call_tool("click", {"element_index": 23})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "driver_exit_1:invalid_arguments")


class FailedStepContinuesTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_step_is_planner_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            created = create_computer_task(
                settings, "在电脑上打开文件管理器",
                direct_mode=True, max_steps=4, max_seconds=30,
            )
            self.assertTrue(created.get("ok"))
            step = create_computer_step(
                settings, created["task_id"], {"action": "type", "text": "exo-open"},
            )
            self.assertTrue(claim_computer_step(settings, step["step_id"], "node").get("ok"))
            self.assertTrue(fail_computer_step(
                settings, step["step_id"], "node", "pid_required_for_type_text",
            ).get("ok"))
            result = await HttpComputerBackend(settings, poll_interval=0.01).execute_step(
                settings, created["task_id"], step["step_id"], {"action": "type"},
            )
        self.assertFalse(result["result_ok"])
        self.assertEqual(result["error"], "pid_required_for_type_text")

    async def test_type_without_pid_uses_observed_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            backend = FakeComputerBackend(settings)
            planner = ScriptedPlanner([
                {"action": "observe", "_mock_pid": 6, "_mock_window_id": 60, "_mock_ax_app": "Xfce4-terminal"},
                {"action": "type", "text": "x"},
            ])
            result = await run_computer_loop(
                settings,
                "在电脑上打开文件管理器",
                planner=planner,
                backend=backend,
                max_steps=6,
                max_seconds=30,
                direct_mode=True,
            )
            typed = [
                entry for entry in backend.driver.transport.log
                if entry.get("action") == "type"
            ]
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(len(typed), 1)
        self.assertEqual(typed[0]["redacted"].get("pid"), 6)
        self.assertEqual(typed[0]["redacted"].get("window_id"), 60)

    async def test_failed_click_cannot_be_marked_done(self) -> None:
        class FailClick:
            def __init__(self) -> None:
                self.actions: list[str] = []

            async def execute_step(self, settings, task_id, step_id, action):
                self.actions.append(str(action.get("action")))
                if action.get("action") == "click":
                    return {"result_ok": False, "error": "driver_exit_1", "action_type": "click"}
                return {"result_ok": True, "action_type": action.get("action")}

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            backend = FailClick()
            planner = ScriptedPlanner([
                {"action": "click", "pid": 4, "window_id": 40, "x": 864, "y": 567},
            ])
            result = await run_computer_loop(
                settings,
                "在电脑上打开文件管理器",
                planner=planner,
                backend=backend,
                max_steps=8,
                max_seconds=30,
                direct_mode=True,
            )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "error")
        self.assertEqual(result.get("blocked_reason"), "unverified_done")
        self.assertIn("click", backend.actions)

    async def test_max_steps_is_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            backend = FakeComputerBackend(settings)
            planner = ScriptedPlanner([
                {"action": "observe"},
                {"action": "observe"},
                {"action": "observe"},
            ])
            result = await run_computer_loop(
                settings,
                "在电脑上打开文件管理器",
                planner=planner,
                backend=backend,
                max_steps=2,
                max_seconds=30,
                direct_mode=True,
            )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "stopped")
        self.assertEqual(result.get("blocked_reason"), "max_steps reached")
        self.assertNotEqual(result.get("summary"), "max_steps reached")

    async def test_opening_observe_happens_before_the_model(self) -> None:
        class Once:
            def __init__(self) -> None:
                self.calls = 0

            async def next_action(self, **_kwargs: object) -> dict:
                self.calls += 1
                return {"action": "done", "summary": "seen"}

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            planner = Once()
            result = await run_computer_loop(
                settings,
                "在电脑上打开文件管理器",
                planner=planner,
                backend=FakeComputerBackend(settings),
                max_steps=4,
                max_seconds=30,
                direct_mode=True,
                open_with_observe=True,
            )
            task = get_computer_task(settings, result["task_id"]) or {}
        types = [step.get("action_type") for step in task.get("trajectory") or []]
        self.assertEqual(types[0], "observe")
        self.assertEqual(planner.calls, 1)
        self.assertEqual(result.get("status"), "done")

    def test_planner_allows_window_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            thread = "11111111-1111-4111-8111-111111111111"
            planner = CodexPlanner(settings, resume_thread_id=thread)
            self.assertEqual(planner.thread_id, thread)
            self.assertIsNone(CodexPlanner(settings, resume_thread_id="not-a-thread").thread_id)
            prompt = planner._build_prompt(
                goal="再点等号",
                observation={},
                trajectory=[],
                steps_used=0,
                max_steps=20,
            )
            follow = planner._build_followup(
                goal="再点等号",
                observation={},
                trajectory=[],
                steps_used=1,
                max_steps=20,
            )
        self.assertNotIn("禁止只输出", prompt)
        self.assertIn("窗口内像素", prompt)
        self.assertIn("只会把窗口放到最前", prompt)
        self.assertIn("窗口内像素", follow)


class NamedFollowupClickTest(unittest.IsolatedAsyncioTestCase):
    async def test_equals_followup_skips_the_model_and_does_not_clear(self) -> None:
        class FrontChat:
            def __init__(self) -> None:
                self.actions: list[dict] = []

            async def execute_step(self, settings, task_id, step_id, action):
                self.actions.append(dict(action))
                if action.get("action") != "observe":
                    return {"result_ok": True, "action_type": action.get("action")}
                if action.get("pid") == 42:
                    return {
                        "result_ok": True,
                        "action_type": "observe",
                        "screenshot_id": "shot-calc",
                        "sha256": hashlib.sha256(b"shot-calc").hexdigest(),
                        "pid": 42,
                        "window_id": 7,
                        "element_hints": [
                            {"label": "Clear", "element_index": 1},
                            {"label": "=", "element_index": 4},
                        ],
                        "windows": _windows(),
                    }
                return {
                    "result_ok": True,
                    "action_type": "observe",
                    "screenshot_id": "shot-chat",
                    "sha256": hashlib.sha256(b"shot-chat").hexdigest(),
                    "pid": 9,
                    "window_id": 3,
                    "element_hints": [{"label": "发送", "element_index": 2}],
                    "windows": _windows(),
                }

        class Explode:
            def __init__(self) -> None:
                self.calls = 0

            async def next_action(self, **_kwargs: object) -> dict:
                self.calls += 1
                raise RuntimeError("planner should not run")

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            backend = FrontChat()
            planner = Explode()
            result = await run_computer_loop(
                settings,
                "再点等号",
                planner=planner,
                backend=backend,
                max_steps=8,
                max_seconds=30,
                direct_mode=True,
            )
        self.assertEqual(result.get("status"), "done", result)
        self.assertIsNone(result.get("blocked_reason"))
        self.assertEqual(planner.calls, 0)
        kinds = [item.get("action") for item in backend.actions]
        self.assertEqual(kinds, ["observe", "observe", "click", "observe"])
        self.assertIsNone(backend.actions[0].get("pid"))
        self.assertEqual(backend.actions[1].get("pid"), 42)
        self.assertEqual(backend.actions[1].get("window_id"), 7)
        self.assertNotIn("target_app", backend.actions[1])
        click = backend.actions[2]
        self.assertEqual(click.get("element_index"), 4)
        self.assertEqual(click.get("_target_label"), "=")


def _windows() -> list[dict]:
    return [
        {
            "app": "Desktop_chat_window.py",
            "title": "Conveyor",
            "pid": 9,
            "window_id": 3,
            "z": 2,
        },
        {
            "app": "gnome-calculator",
            "title": "Calculator",
            "pid": 42,
            "window_id": 7,
            "z": 1,
        },
    ]


class BlockedKeywordsPolicyTest(unittest.TestCase):
    def test_default_blocks_password_and_financial(self) -> None:
        from desktop_computer_requests import contains_blocked_keyword
        settings = _settings(Path(tempfile.mkdtemp()))
        self.assertEqual(contains_blocked_keyword(settings, "enter password to login"), "password")
        self.assertEqual(contains_blocked_keyword(settings, "open bank account"), "bank")
        self.assertEqual(contains_blocked_keyword(settings, "process payment"), "payment")
        self.assertEqual(contains_blocked_keyword(settings, "请输入支付密码"), "支付")
        self.assertEqual(contains_blocked_keyword(settings, "向好友转账"), "转账")
        self.assertEqual(contains_blocked_keyword(settings, "网银付款"), "付款")

    def test_allow_login_passwords_unblocks_login_but_guards_payment(self) -> None:
        from desktop_computer_requests import contains_blocked_keyword
        temp = Path(tempfile.mkdtemp())
        settings = _settings(temp, conveyor_computer_allow_login_passwords=True)
        # Login passwords allowed
        self.assertIsNone(contains_blocked_keyword(settings, "enter password to continue"))
        self.assertIsNone(contains_blocked_keyword(settings, "use passcode 123456"))
        self.assertIsNone(contains_blocked_keyword(settings, "输入登录密码 admin123"))
        self.assertIsNone(contains_blocked_keyword(settings, "输入开机口令"))
        # Financial / payment / transfer strictly blocked
        self.assertEqual(contains_blocked_keyword(settings, "请输入支付密码"), "支付")
        self.assertEqual(contains_blocked_keyword(settings, "进行银行转账"), "转账")
        self.assertEqual(contains_blocked_keyword(settings, "enter payment password"), "payment")
        self.assertEqual(contains_blocked_keyword(settings, "bank passcode"), "bank")
        self.assertEqual(contains_blocked_keyword(settings, "delete account"), "delete account")

    def test_custom_blocked_keywords_omitting_password_allows_login(self) -> None:
        from desktop_computer_requests import contains_blocked_keyword
        temp = Path(tempfile.mkdtemp())
        settings = _settings(
            temp,
            conveyor_computer_allow_login_passwords=False,
            conveyor_computer_blocked_keywords=("bank", "payment"),
        )
        self.assertIsNone(contains_blocked_keyword(settings, "enter password to continue"))
        self.assertEqual(contains_blocked_keyword(settings, "open bank app"), "bank")
        self.assertEqual(contains_blocked_keyword(settings, "向该账户转账"), "转账")


if __name__ == "__main__":
    unittest.main()
