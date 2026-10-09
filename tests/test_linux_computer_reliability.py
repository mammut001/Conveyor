"""Regression tests for the shared Linux browser control path."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import Settings
from desktop_computer_loop import run_computer_loop
from desktop_computer_planner import ScriptedPlanner
from desktop_cua import LocalCuaTransport
from desktop_linux_browser import LinuxBrowserController


def _settings(root: Path) -> Settings:
    (root / "tasks").mkdir()
    (root / "workspace").mkdir()
    return Settings(
        telegram_bot_token="test", telegram_allowed_user_id=123,
        codex_workspace_root=root / "workspace", codex_bin="codex",
        codex_task_root=root / "tasks", codex_model=None,
        codex_timeout_seconds=30, codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", chat_mode="off",
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_always_direct=True,
    )


def _cp(*argv: str, out: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, rc, out, "")


class LinuxBrowserTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.linux_process_app", return_value="Firefox")
    @mock.patch("desktop_linux_browser.shutil.which")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_existing_browser_is_verified_and_not_relaunched(self, popen, which, _process):
        which.return_value = "/usr/bin/xdotool"
        controller = LinuxBrowserController()
        observed: list[tuple] = []

        def run(*argv):
            observed.append(argv)
            if argv[1] == "search":
                return _cp(*argv, out="4242\n")
            if argv[1] == "windowactivate":
                return _cp(*argv)
            if argv[1] == "getactivewindow":
                return _cp(*argv, out="4242\n")
            if argv[1] == "getwindowpid":
                return _cp(*argv, out="123\n")
            return _cp(*argv, rc=1)

        controller._run = run
        result = controller.ensure("Firefox")
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 123, "window_id": 4242})
        popen.assert_not_called()
        self.assertTrue(any(cmd[1] == "windowactivate" for cmd in observed))

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_focus_failure_does_not_launch_duplicate(self, popen, _which):
        controller = LinuxBrowserController()
        def run(*argv):
            if argv[1] == "search":
                return _cp(*argv, out="4242\n")
            if argv[1] == "getactivewindow":
                return _cp(*argv, out="5555\n")
            return _cp(*argv)
        controller._run = run
        self.assertEqual(controller.ensure("Firefox")["error"], "browser_activate_failed")
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/firefox")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    @mock.patch("desktop_linux_browser.time.sleep")
    def test_missing_browser_launches_fixed_binary_and_verifies_mapping(self, sleeper, popen, _which):
        controller = LinuxBrowserController()
        controller._window_ids = mock.Mock(side_effect=[[], ["500"]])
        controller._focus = mock.Mock(return_value=999)
        result = controller.ensure("Firefox")
        self.assertEqual(result["pid"], 999)
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/firefox", "--no-remote", "--profile"])
        self.assertEqual(command[-2:], ["--new-window", "about:blank"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_allowlist_and_blocklist_fail_closed(self, popen, _which):
        controller = LinuxBrowserController()
        self.assertEqual(controller.ensure("Firefox", allowed_apps=("Calculator",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("Firefox", blocked_apps=("Firefox",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("Not-A-Real-App")["error"], "browser_not_supported")
        popen.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.ensure")
    def test_cua_uses_x11_mapping_not_driver_exact_name(self, ensure):
        ensure.return_value = {"ok": True, "pid": 777, "name": "Firefox"}
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            driver._call_tool = mock.Mock(side_effect=AssertionError("no list_apps"))
            action = {"action": "observe", "target_app": "Firefox"}
            self.assertIsNone(driver._prepare_target_app(action))
            self.assertEqual(action["pid"], 777)
            driver._call_tool.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.ensure")
    def test_cua_preflight_respects_controller_refusal(self, ensure):
        ensure.return_value = {"ok": False, "error": "browser_binary_missing"}
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            action = {"action": "observe", "ensure_browser": True}
            self.assertEqual(driver._prepare_target_app(action), "browser_binary_missing")
            self.assertNotIn("pid", action)


class _Sequence:
    def __init__(self, *actions):
        self.actions = list(actions)

    async def next_action(self, **_kwargs):
        return self.actions.pop(0) if self.actions else {"action": "done", "summary": "complete"}


class _Desktop:
    def __init__(self, results):
        self.results = list(results)
        self.actions = []

    async def execute_step(self, settings, task_id, step_id, action):
        self.actions.append(dict(action))
        return self.results.pop(0) if self.results else {
            "result_ok": True, "action_type": action["action"], "sha256": "same"
        }


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_browser_goal_bootstraps_once_and_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([{
                "result_ok": True, "action_type": "observe", "sha256": "first",
                "screenshot_id": "fake", "active_app": "Firefox",
            }])
            result = await run_computer_loop(
                _settings(Path(temp)), "Check weather in current browser",
                planner=_Sequence({"action": "done", "summary": "observed"}),
                backend=backend, max_steps=5, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertTrue(backend.actions[0].get("ensure_browser"))
            self.assertEqual(result["status"], "done")

    async def test_failed_target_forces_untargeted_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([
                {"result_ok": False, "error": "browser_activate_failed"},
                {"result_ok": True, "action_type": "observe", "sha256": "new", "screenshot_id": "fake", "active_app": "Firefox"},
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Open Firefox to check weather",
                planner=_Sequence({"action": "done", "summary": "observed"}),
                backend=backend, max_steps=5, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertEqual(result["status"], "done")
            self.assertEqual(backend.actions[0].get("target_app"), "Firefox")
            self.assertEqual(backend.actions[1], {"action": "observe"})

    async def test_cannot_report_success_with_application_finder_foreground(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([
                {"result_ok": False, "error": "target_app_not_found"},
                {"result_ok": True, "action_type": "observe", "sha256": "xfce",
                 "screenshot_id": "fake", "active_app": "Application Finder"},
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Open Firefox and read weather",
                planner=_Sequence({"action": "done", "summary": "Firefox opened"},
                                  {"action": "done", "summary": "Firefox opened"}),
                backend=backend, max_steps=8, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["blocked_reason"], "unverified_browser_window")

    async def test_repeated_visual_stall_stops_before_step_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([
                {"result_ok": True, "action_type": "observe", "sha256": "same", "screenshot_id": "fake"},
                {"result_ok": True, "action_type": "click"},
                {"result_ok": True, "action_type": "observe", "sha256": "same", "screenshot_id": "fake"},
                {"result_ok": True, "action_type": "click"},
                {"result_ok": True, "action_type": "observe", "sha256": "same", "screenshot_id": "fake"},
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Check weather in browser",
                planner=_Sequence({"action": "click", "x": 3, "y": 4},
                                  {"action": "click", "x": 3, "y": 4}),
                backend=backend, max_steps=20, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["blocked_reason"], "no_visual_progress")
            self.assertLess(result["steps_used"], 20)


if __name__ == "__main__":
    unittest.main()
