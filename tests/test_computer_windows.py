"""Window list for the desktop planner, and a failed step that continues."""
from __future__ import annotations

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
from desktop_computer_planner import ScriptedPlanner
from desktop_computer_requests import (
    claim_computer_step,
    create_computer_step,
    create_computer_task,
    fail_computer_step,
    validate_computer_result,
)
from desktop_cua import LocalCuaTransport, _window_local_point, summarize_windows


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


if __name__ == "__main__":
    unittest.main()
