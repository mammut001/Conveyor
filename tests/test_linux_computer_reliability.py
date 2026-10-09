"""Regression tests for the shared Linux browser control path."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
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


def _xprop_window(
    instance: str = "Navigator",
    klass: str = "Firefox",
    state: str = "Normal",
    window_type: str = "_NET_WM_WINDOW_TYPE_NORMAL",
    *,
    include_state: bool = True,
    include_type: bool = True,
) -> str:
    """Faithful ``xprop -id WINDOW WM_CLASS WM_STATE _NET_WM_WINDOW_TYPE`` stdout."""
    lines = [f'WM_CLASS(STRING) = "{instance}", "{klass}"']
    if include_state:
        lines.extend([
            "WM_STATE(WM_STATE):",
            f"\t\twindow state: {state}",
            "\t\ticon window: 0x0",
        ])
    if include_type:
        lines.append(f"_NET_WM_WINDOW_TYPE(ATOM) = {window_type}")
    return "\n".join(lines) + "\n"


def _scripted_run(script):
    """Return an xdotool/xprop stub. `script` maps the subcommand to stdout.

    ``xprop`` may be a string, a list consumed in order, or a dict keyed by
    window id (``xprop -id WINDOW ...``).
    """

    def run(*argv):
        if argv and argv[0] == "xprop":
            key = "xprop"
            wid = argv[2] if len(argv) > 2 and argv[1] == "-id" else ""
        else:
            key = argv[1]
            wid = ""
        if key not in script:
            return _cp(*argv, rc=1)
        value = script[key]
        if isinstance(value, dict):
            value = value.get(wid, "")
        elif isinstance(value, list):
            value = value.pop(0) if value else ""
        if value is None:
            return _cp(*argv, rc=1)
        text = str(value)
        return _cp(*argv, out=text if text.endswith("\n") or text == "" else text + "\n")

    return run


class LinuxBrowserTest(unittest.TestCase):
    def _controller(self, script, process="Firefox"):
        controller = LinuxBrowserController()
        controller._run = _scripted_run(script)
        self._process = mock.patch(
            "desktop_linux_browser.linux_process_app", return_value=process,
        )
        self._process.start()
        self.addCleanup(self._process.stop)
        return controller

    def test_env_argument_is_isolated_from_process_environ(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":1"}):
            controller = LinuxBrowserController({
                "DISPLAY": ":109", "XAUTHORITY": "/tmp/private-xauth", "PATH": "/usr/bin",
            })
            os.environ["DISPLAY"] = ":0"
            self.assertEqual(controller.env["DISPLAY"], ":109")
            self.assertEqual(os.environ["DISPLAY"], ":0")
            self.assertIsNot(controller.env, os.environ)

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_existing_browser_is_verified_and_not_relaunched(self, popen, _which):
        observed = []
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "4242",
            "xprop": _xprop_window(),
            "getwindowpid": "123",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "4242",
        })
        real_run = controller._run

        def run(*argv):
            observed.append(argv)
            return real_run(*argv)

        controller._run = run
        result = controller.ensure("Firefox")
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 123, "window_id": 4242})
        popen.assert_not_called()
        search = next(cmd for cmd in observed if cmd[1] == "search")
        self.assertNotIn("--onlyvisible", search)
        self.assertIn("windowmap", [cmd[1] for cmd in observed])
        self.assertLess(
            [cmd[0] for cmd in observed].index("xprop"),
            [cmd[1] for cmd in observed].index("windowactivate"),
        )

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_focus_failure_does_not_launch_duplicate(self, popen, _which):
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "4242",
            "xprop": _xprop_window(),
            "getwindowpid": "123",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "5555",
        })
        self.assertEqual(controller.ensure("Firefox")["error"], "browser_activate_failed")
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_spoofed_class_is_not_focused_or_launched(self, popen, _which):
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "4242",
            "xprop": _xprop_window(),
            "getwindowpid": "9",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "4242",
        }, process="bash")
        calls = []
        real = controller._run

        def run(*argv):
            calls.append(argv)
            return real(*argv)

        controller._run = run
        self.assertEqual(controller.ensure("Firefox")["error"], "browser_activate_failed")
        popen.assert_not_called()
        self.assertFalse(any(cmd[1] == "windowactivate" for cmd in calls))

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_helper_and_dialog_are_not_browser_targets(self, popen, _which):
        helper = _xprop_window(instance="Firefox", klass="firefox_firefox", include_state=False, include_type=False)
        dialog = _xprop_window(
            instance="Firefox", klass="firefox_firefox",
            window_type="_NET_WM_WINDOW_TYPE_DIALOG",
        )
        real = _xprop_window(instance="Firefox", klass="firefox_firefox")
        activated = []
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "10\n11\n12",
            "xprop": {"10": helper, "11": dialog, "12": real},
            "getwindowpid": "123",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "12",
        })
        real_run = controller._run

        def run(*argv):
            if len(argv) > 1 and argv[1] == "windowactivate":
                activated.append(argv[-1])
            return real_run(*argv)

        controller._run = run
        result = controller.ensure("Firefox")
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 123, "window_id": 12})
        self.assertEqual(activated, ["12"])
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_snap_navigator_window_is_focused_helper_and_dialog_are_not(self, popen, _which):
        """Empirical Snap strings from a normal Firefox window on the VPS."""
        from desktop_linux_browser import wm_class_matches, xprop_browser_target

        real = (
            'WM_CLASS(STRING) = "Navigator", "firefox_firefox"\n'
            "WM_STATE(WM_STATE):\n"
            "\t\twindow state: Normal\n"
            "\t\ticon window: 0x0\n"
            "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n"
        )
        helper = 'WM_CLASS(STRING) = "firefox_firefox", "Firefox_firefox"\n'
        dialog = (
            'WM_CLASS(STRING) = "Firefox", "firefox_firefox"\n'
            "WM_STATE(WM_STATE):\n"
            "\t\twindow state: Normal\n"
            "\t\ticon window: 0x0\n"
            "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DIALOG\n"
        )
        self.assertTrue(wm_class_matches("Firefox", "Navigator", "firefox_firefox"))
        self.assertFalse(wm_class_matches("Firefox", "firefox_firefox", "Firefox_firefox"))
        self.assertTrue(xprop_browser_target(real, "Firefox"))
        self.assertFalse(xprop_browser_target(helper, "Firefox"))
        self.assertFalse(xprop_browser_target(dialog, "Firefox"))
        activated = []
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "10\n11\n12",
            "xprop": {"10": helper, "11": dialog, "12": real},
            "getwindowpid": "214271",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "12",
        })
        real_run = controller._run

        def run(*argv):
            if len(argv) > 1 and argv[1] == "windowactivate":
                activated.append(argv[-1])
            return real_run(*argv)

        controller._run = run
        result = controller.ensure("Firefox")
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 214271, "window_id": 12})
        self.assertEqual(activated, ["12"])
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_minimized_snap_firefox_is_mapped(self, popen, _which):
        controller = self._controller({
            "getdisplaygeometry": "100 100",
            "search": "77",
            "xprop": _xprop_window(instance="Firefox", klass="firefox_firefox", state="Iconic"),
            "getwindowpid": "123",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "77",
        })
        calls = []
        real_run = controller._run

        def run(*argv):
            calls.append(argv)
            return real_run(*argv)

        controller._run = run
        result = controller.ensure("Firefox")
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 123, "window_id": 77})
        self.assertTrue(any(cmd[1] == "windowmap" and cmd[-1] == "77" for cmd in calls))
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser._trusted_browser_binary", return_value="/usr/bin/firefox")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    @mock.patch("desktop_linux_browser.time.sleep")
    def test_launch_loop_uses_real_focus_after_delayed_mapping(self, sleeper, popen, _binary, _which):
        searches = {"n": 0}

        def run(*argv):
            if argv[1] == "getdisplaygeometry":
                return _cp(*argv, out="100 100\n")
            if argv[1] == "search":
                searches["n"] += 1
                if searches["n"] < 3:
                    return _cp(*argv, out="")
                return _cp(*argv, out="500\n")
            if argv[0] == "xprop":
                return _cp(*argv, out=_xprop_window())
            if argv[1] == "getwindowpid":
                return _cp(*argv, out="55\n")
            if argv[1] in {"windowmap", "windowactivate"}:
                return _cp(*argv)
            if argv[1] == "getactivewindow":
                return _cp(*argv, out="500\n")
            return _cp(*argv, rc=1)

        controller = LinuxBrowserController()
        controller._run = run
        home = Path(tempfile.mkdtemp())
        with mock.patch("desktop_linux_browser.linux_process_app", return_value="Firefox"), \
                mock.patch("desktop_linux_browser.Path.home", return_value=home):
            result = controller.ensure("Firefox", timeout=5)
        self.assertEqual(result, {"ok": True, "name": "Firefox", "pid": 55, "window_id": 500})
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/firefox")
        self.assertEqual(command[1:3], ["--no-remote", "--profile"])
        self.assertIn("_99", command[3])
        self.assertEqual(command[-2:], ["--new-window", "about:blank"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertGreaterEqual(searches["n"], 3)
        sleeper.assert_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99", "XAUTHORITY": "/no/such/xauth"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_unreachable_display_does_not_launch(self, popen, _which):
        controller = LinuxBrowserController()
        controller._run = mock.Mock(side_effect=AssertionError("xdotool"))
        self.assertEqual(controller.ensure("Firefox")["error"], "browser_display_unreachable")
        popen.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_allowlist_and_blocklist_fail_closed(self, popen, _which):
        controller = LinuxBrowserController()
        controller._run = mock.Mock(side_effect=AssertionError("focused a blocked browser"))
        self.assertEqual(controller.ensure("Firefox", allowed_apps=("Calculator",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("Firefox", blocked_apps=("Firefox",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("chrome", blocked_apps=("Google Chrome",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("Not-A-Real-App")["error"], "browser_not_supported")
        self.assertEqual(controller.ensure("Firefox", blocked_apps=("Browser",))["error"], "browser_disallowed")
        self.assertEqual(controller.ensure("Browser", blocked_apps=("Browser",))["error"], "browser_disallowed")
        self.assertEqual(
            controller.ensure("Firefox", allowed_apps=("Browser",), blocked_apps=("Firefox",))["error"],
            "browser_disallowed",
        )
        popen.assert_not_called()
        controller._run.assert_not_called()

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_generic_browser_allow_can_select_chrome_when_firefox_is_blocked(self, popen, _which):
        controller = LinuxBrowserController()
        searches: list[tuple] = []

        def run(*argv):
            if len(argv) > 1 and argv[1] == "search":
                searches.append(argv)
                return _cp(*argv, out="")
            if len(argv) > 1 and argv[1] == "getdisplaygeometry":
                return _cp(*argv, out="100 100\n")
            return _cp(*argv, rc=1)

        controller._run = run
        controller._binary = lambda _name: None
        denied = controller.ensure("Firefox", allowed_apps=("Browser",), blocked_apps=("Firefox",))
        self.assertEqual(denied["error"], "browser_disallowed")
        self.assertEqual(searches, [])
        selected = controller.ensure(
            "Browser", allowed_apps=("Google Chrome",), blocked_apps=("Firefox",),
        )
        self.assertEqual(selected["error"], "browser_binary_missing")
        self.assertTrue(searches)
        self.assertTrue(all("firefox" not in " ".join(call).lower() for call in searches))
        self.assertTrue(any("Chrome" in " ".join(call) for call in searches))
        popen.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.ensure")
    def test_cua_uses_x11_mapping_not_driver_exact_name(self, ensure):
        ensure.return_value = {"ok": True, "pid": 777, "name": "Firefox", "window_id": 42}
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            driver._call_tool = mock.Mock(side_effect=AssertionError("no list_apps"))
            action = {"action": "observe", "target_app": "Firefox"}
            self.assertIsNone(driver._prepare_target_app(action))
            self.assertEqual(action["pid"], 777)
            self.assertEqual(action["window_id"], 42)
            driver._call_tool.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.ensure")
    def test_contradictory_pid_is_not_retargeted(self, ensure):
        ensure.return_value = {"ok": False, "error": "target_identity_conflict"}
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            action = {"action": "observe", "target_app": "Firefox", "pid": 1, "window_id": 2}
            self.assertEqual(driver._prepare_target_app(action), "target_identity_conflict")
            self.assertEqual(ensure.call_args.kwargs["expected_pid"], 1)
            self.assertEqual(ensure.call_args.kwargs["expected_window"], 2)
            self.assertEqual(action["pid"], 1)

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.ensure")
    def test_model_ensure_browser_string_does_not_launch(self, ensure):
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            action = {"action": "observe", "ensure_browser": "firefox; touch /tmp/x"}
            self.assertIsNone(driver._prepare_target_app(action))
            ensure.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "linux")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.keyboard_target_ready", return_value=False)
    def test_linux_type_refuses_when_focus_moved(self, _ready):
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            driver._call_tool = mock.Mock(side_effect=AssertionError("typed into the wrong window"))
            result = driver._type_text({"action": "type", "text": "hi", "pid": 4, "window_id": 8}, "n")
            self.assertEqual(result["error"], "keyboard_target_not_foreground")
            driver._call_tool.assert_not_called()

    @mock.patch("desktop_cua.sys.platform", "darwin")
    @mock.patch("desktop_linux_browser.LinuxBrowserController.keyboard_target_ready",
                side_effect=AssertionError("mac checks x11 focus"))
    def test_mac_type_keeps_background_pid_delivery(self, _ready):
        with tempfile.TemporaryDirectory() as temp:
            driver = LocalCuaTransport("cua-driver mcp", settings=_settings(Path(temp)))
            driver._call_tool = mock.Mock(return_value={"ok": True, "data": {}})
            result = driver._type_text({"action": "type", "text": "hi", "pid": 4}, "n")
            self.assertTrue(result["result_ok"])
            self.assertEqual(driver._call_tool.call_args.args[0], "type_text")

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
    async def test_open_firefox_verifies_foreground_once(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([{
                "result_ok": True, "action_type": "observe", "sha256": "first",
                "screenshot_id": "fake", "active_app": "Firefox",
            }])
            result = await run_computer_loop(
                _settings(Path(temp)), "Open Firefox",
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
                _settings(Path(temp)), "Open Firefox",
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
            self.assertTrue(any(item.get("ensure_browser") for item in backend.actions[1:]))

    async def test_different_actions_with_the_same_image_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            same = {"result_ok": True, "sha256": "same", "screenshot_id": "fake", "active_app": "TextEdit"}
            backend = _Desktop([
                dict(same, action_type="observe"),
                {"result_ok": True, "action_type": "hotkey"},
                dict(same, action_type="observe"),
                {"result_ok": True, "action_type": "type"},
                dict(same, action_type="observe"),
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Type a note",
                planner=_Sequence({"action": "hotkey", "keys": ["ctrl", "l"]},
                                  {"action": "type", "text": "hello"},
                                  {"action": "done", "summary": "typed"}),
                backend=backend, max_steps=12, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertEqual(result["status"], "done")
            self.assertNotIn("hello", str(result))

    async def test_loading_title_is_not_a_stall_or_a_finished_page(self):
        import desktop_computer_loop as loop

        clock = {"now": 0.0}

        def monotonic():
            return clock["now"]

        async def sleep(seconds):
            clock["now"] += float(seconds)

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", monotonic), \
                mock.patch.object(loop, "_sleep", sleep):
            frame = {
                "result_ok": True, "sha256": "same", "screenshot_id": "fake",
                "active_app": "Firefox", "window_title": "Loading…",
            }
            backend = _Desktop([
                dict(frame, action_type="observe"),
                {"result_ok": True, "action_type": "click"},
                dict(frame, action_type="observe"),
                {"result_ok": True, "action_type": "click"},
                dict(frame, action_type="observe"),
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Check weather in browser",
                planner=_Sequence({"action": "click", "x": 1, "y": 1},
                                  {"action": "click", "x": 1, "y": 1},
                                  {"action": "done", "summary": "weather"},
                                  {"action": "done", "summary": "weather"}),
                backend=backend, max_steps=40, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertNotEqual(result["blocked_reason"], "no_visual_progress")
            self.assertNotEqual(result["status"], "done")

    async def test_about_blank_and_network_error_are_not_success(self):
        titles = (
            "about:blank — Mozilla Firefox",
            "Problem loading page",
            "Mozilla Firefox",
            "Google Chrome",
            "",
        )
        for title in titles:
            with tempfile.TemporaryDirectory() as temp:
                frame = {
                    "result_ok": True, "action_type": "observe", "sha256": "blank",
                    "screenshot_id": "fake", "active_app": "Firefox", "window_title": title,
                }
                backend = _Desktop([dict(frame), dict(frame)])
                result = await run_computer_loop(
                    _settings(Path(temp)), "Check weather in current browser",
                    planner=_Sequence({"action": "done", "summary": "sunny"},
                                      {"action": "done", "summary": "sunny"}),
                    backend=backend, max_steps=6, max_seconds=30, direct_mode=True,
                    open_with_observe=True,
                )
                self.assertEqual(result["status"], "error", title)
                self.assertNotEqual(result["status"], "done", title)
                self.assertEqual(result["blocked_reason"], "unverified_browser_window", title)

    async def test_loaded_page_missing_title_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = {
                "result_ok": True, "action_type": "observe", "sha256": "blank",
                "screenshot_id": "fake", "active_app": "Firefox",
                "windows": [{"app": "Firefox", "title": "Problem loading page", "pid": 5, "window_id": 9, "z": 1}],
                "pid": 5, "window_id": 9,
            }
            absent = {
                "result_ok": True, "action_type": "observe", "sha256": "blank",
                "screenshot_id": "fake", "active_app": "Firefox",
            }
            settings = _settings(Path(temp))
            for label, rows in (("derived", [dict(frame), dict(frame)]), ("absent", [dict(absent), dict(absent)])):
                backend = _Desktop(rows)
                result = await run_computer_loop(
                    settings, "Check the weather webpage",
                    planner=_Sequence({"action": "done", "summary": "sunny"},
                                      {"action": "done", "summary": "sunny"}),
                    backend=backend, max_steps=6, max_seconds=30, direct_mode=True,
                    open_with_observe=True,
                )
                self.assertEqual(result["status"], "error", label)
                self.assertEqual(result["blocked_reason"], "unverified_browser_window", label)

    async def test_early_done_without_screenshot_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([])
            result = await run_computer_loop(
                _settings(Path(temp)), "Check weather in browser",
                planner=_Sequence({"action": "done", "summary": "done"},
                                  {"action": "done", "summary": "done"}),
                backend=backend, max_steps=4, max_seconds=30, direct_mode=True,
            )
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["blocked_reason"], "unverified_done")
            self.assertEqual(backend.actions, [])

    async def test_observe_only_goal_does_not_launch_a_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([{
                "result_ok": True, "action_type": "observe", "sha256": "s",
                "screenshot_id": "fake", "active_app": "xfce4-panel",
            }])
            result = await run_computer_loop(
                _settings(Path(temp)), "observe the browser only and do not click",
                planner=_Sequence({"action": "click", "x": 1, "y": 1}),
                backend=backend, max_steps=4, max_seconds=30, direct_mode=True,
                open_with_observe=True,
            )
            self.assertEqual(result["status"], "done")
            self.assertNotIn("ensure_browser", backend.actions[0])
            self.assertEqual([item["action"] for item in backend.actions], ["observe"])

    async def test_failure_count_survives_observe_and_a_later_success(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = _Desktop([
                {"result_ok": False, "error": "click_failed"},
                {"result_ok": True, "action_type": "observe", "sha256": "a", "screenshot_id": "a"},
                {"result_ok": False, "error": "click_failed"},
                {"result_ok": True, "action_type": "observe", "sha256": "b", "screenshot_id": "b"},
                {"result_ok": True, "action_type": "click"},
                {"result_ok": True, "action_type": "observe", "sha256": "c", "screenshot_id": "c"},
                {"result_ok": False, "error": "click_failed"},
            ])
            result = await run_computer_loop(
                _settings(Path(temp)), "Press the button",
                planner=_Sequence(
                    {"action": "click", "x": 2, "y": 2},
                    {"action": "click", "x": 2, "y": 2},
                    {"action": "click", "x": 2, "y": 2},
                    {"action": "click", "x": 2, "y": 2},
                ),
                backend=backend, max_steps=12, max_seconds=30, direct_mode=True,
            )
            self.assertEqual(result["blocked_reason"], "repeated_action_failure")

    async def test_takeover_discards_the_old_plan_and_observes_first(self):
        from desktop_computer_requests import create_computer_task
        from human_takeover import HumanTakeoverStore

        with tempfile.TemporaryDirectory() as temp:
            settings = _settings(Path(temp))
            created = create_computer_task(
                settings, "Press the button", direct_mode=True, max_steps=6, max_seconds=30,
            )
            leases = HumanTakeoverStore(settings)
            lease = leases.start(reason="operator_requested", scope="default", ttl_seconds=30)
            seen = []

            class Planner:
                async def next_action(self, **_kwargs):
                    seen.append("plan")
                    return {"action": "click", "x": 1, "y": 1} if len(seen) == 1 else {"action": "done", "summary": "ok"}

            backend = _Desktop([
                {"result_ok": True, "action_type": "observe", "sha256": "new", "screenshot_id": "fresh", "active_app": "TextEdit"},
                {"result_ok": True, "action_type": "click"},
                {"result_ok": True, "action_type": "observe", "sha256": "newer", "screenshot_id": "fresh2", "active_app": "TextEdit"},
            ])

            async def scenario():
                running = asyncio.create_task(run_computer_loop(
                    settings, "Press the button", planner=Planner(), backend=backend,
                    max_steps=6, max_seconds=30, direct_mode=True, task_id=created["task_id"],
                ))
                await asyncio.sleep(0.4)
                self.assertEqual(backend.actions, [])
                self.assertEqual(seen, [])
                leases.complete(lease["id"])
                return await asyncio.wait_for(running, timeout=20)

            result = await scenario()
            self.assertEqual(backend.actions[0]["action"], "observe")
            self.assertNotIn("target_app", backend.actions[0])
            self.assertEqual(result["status"], "done")

    async def test_browser_submit_settles_before_done_and_skips_non_browser(self):
        import desktop_computer_loop as loop

        clock = {"now": 5000.0}

        def monotonic():
            return clock["now"]

        async def sleep(seconds):
            clock["now"] += float(seconds)

        class Backend:
            def __init__(self, app: str) -> None:
                self.app = app
                self.actions: list[dict] = []

            async def execute_step(self, settings, task_id, step_id, action):
                self.actions.append(dict(action))
                if action.get("action") != "observe":
                    return {"result_ok": True, "action_type": action.get("action")}
                entered = any(
                    item.get("action") == "hotkey" and item.get("keys") == ["enter"]
                    for item in self.actions
                )
                fresh = entered and clock["now"] >= 5001.3
                sha = "new" if fresh else "old"
                return {
                    "result_ok": True, "action_type": "observe", "sha256": sha,
                    "screenshot_id": sha, "active_app": self.app,
                    "browser_page_state": "loaded", "pid": 5, "window_id": 9,
                }

        calls: list[tuple] = []

        class Planner:
            def __init__(self, backend) -> None:
                self.backend = backend
                self.sent_enter = False

            async def next_action(self, **kwargs):
                obs = kwargs.get("observation") or {}
                calls.append((clock["now"], obs.get("sha256"), obs.get("active_app")))
                if self.backend.app == "Calculator":
                    if not any(item.get("action") == "click" for item in self.backend.actions):
                        return {"action": "click", "x": 1, "y": 1}
                    return {"action": "done", "summary": "pressed"}
                if not self.sent_enter:
                    self.sent_enter = True
                    return {"action": "hotkey", "keys": ["enter"]}
                return {"action": "done", "summary": "settled-new"}

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", monotonic), \
                mock.patch.object(loop, "_sleep", sleep):
            backend = Backend("Firefox")
            started = time.monotonic()
            result = await run_computer_loop(
                _settings(Path(temp)), "Open the webpage",
                planner=Planner(backend), backend=backend, max_steps=16, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["summary"], "settled-new")
        self.assertLess(elapsed, 1.0)
        after = [item for item in calls if item[0] >= 5001.0]
        self.assertTrue(after)
        self.assertTrue(all(item[1] == "new" for item in after))
        observes = [item for item in backend.actions if item.get("action") == "observe"]
        self.assertGreaterEqual(sum(1 for _ in observes), 3)
        new_obs = [
            item for item in backend.actions
            if item.get("action") == "observe"
        ]
        enter_at = next(i for i, item in enumerate(backend.actions) if item.get("keys") == ["enter"])
        trailed = backend.actions[enter_at + 1:]
        self.assertTrue(all(item.get("action") == "observe" for item in trailed))
        self.assertGreaterEqual(len(trailed), 3)
        self.assertNotIn("http://", str(result))

        clock["now"] = 8000.0
        calls.clear()
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", monotonic), \
                mock.patch.object(loop, "_sleep", sleep):
            calc = Backend("Calculator")
            before = clock["now"]
            result = await run_computer_loop(
                _settings(Path(temp)), "Press the button",
                planner=Planner(calc), backend=calc, max_steps=8, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
            self.assertEqual(result["status"], "done")
            self.assertEqual(clock["now"], before)
            self.assertEqual(
                [item.get("action") for item in calc.actions],
                ["observe", "click", "observe"],
            )

        clock["now"] = 9000.0
        focus_calls: list[float] = []

        class Keys:
            def __init__(self) -> None:
                self.n = 0

            async def next_action(self, **_kwargs):
                focus_calls.append(clock["now"])
                self.n += 1
                if self.n == 1:
                    return {"action": "hotkey", "keys": ["ctrl", "l"]}
                if self.n == 2:
                    return {"action": "type", "text": "http://127.0.0.1/run-02.html"}
                return {"action": "done", "summary": "typed"}

        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", monotonic), \
                mock.patch.object(loop, "_sleep", sleep):
            page = Backend("Firefox")
            result = await run_computer_loop(
                _settings(Path(temp)), "Open the webpage",
                planner=Keys(), backend=page, max_steps=10, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
        self.assertEqual(result["status"], "error")
        self.assertIn("browser_navigation_not_submitted", result["blocked_reason"])
        self.assertEqual(focus_calls[1] - focus_calls[0], 0.0)
        self.assertNotIn("run-02.html", str(result))


class IdentityTest(unittest.TestCase):
    def test_process_names_are_exact_and_truncation_is_bounded(self):
        from desktop_linux_browser import app_from_process_names

        self.assertEqual(app_from_process_names("firefox", "firefox"), "Firefox")
        self.assertEqual(app_from_process_names(None, "google-chrome-s"), "Google Chrome")
        self.assertEqual(app_from_process_names("chrome-helper", None), "Unknown")
        self.assertEqual(app_from_process_names("notfirefox", "notfirefox"), "Unknown")
        self.assertEqual(app_from_process_names("chromium", None), "Chromium")
        self.assertEqual(app_from_process_names(None, "chrome"), "Google Chrome")

    def test_spoofed_exe_is_not_a_browser_and_terminals_map(self):
        from desktop_linux_browser import linux_process_app

        spoof = Path(tempfile.mkdtemp()) / "firefox"
        spoof.write_text("#!/bin/sh\n", encoding="utf-8")
        spoof.chmod(0o755)
        with mock.patch("desktop_linux_browser._proc_exe", return_value=str(spoof)), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="firefox"):
            self.assertEqual(linux_process_app(9), "Unknown")
        with mock.patch("desktop_linux_browser._proc_exe", return_value="/usr/bin/gnome-terminal"), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="gnome-terminal"):
            self.assertEqual(linux_process_app(9), "Terminal")
        with mock.patch("desktop_linux_browser._proc_exe", return_value=None), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="firefox"):
            self.assertEqual(linux_process_app(9), "Unknown")
        with mock.patch("desktop_linux_browser._proc_exe", return_value="/tmp/firefox"), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="firefox"):
            self.assertEqual(linux_process_app(9), "Unknown")
        with mock.patch("desktop_linux_browser._proc_exe", return_value="/usr/bin/gedit"), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="gedit"):
            self.assertEqual(linux_process_app(9), "gedit")
        with mock.patch("desktop_linux_browser._proc_exe", return_value="/usr/bin/firefox"), \
                mock.patch("desktop_linux_browser._path_is_trusted", return_value=True), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="bash"):
            self.assertEqual(linux_process_app(9), "Firefox")

    @mock.patch.dict(os.environ, {"DISPLAY": ":99"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    def test_spoofed_exe_and_class_never_focus_or_launch(self, popen, _which):
        spoof = Path(tempfile.mkdtemp()) / "firefox"
        spoof.write_text("#!/bin/sh\n", encoding="utf-8")
        spoof.chmod(0o755)
        controller = LinuxBrowserController()
        controller._run = _scripted_run({
            "getdisplaygeometry": "100 100",
            "search": "4242",
            "xprop": _xprop_window(),
            "getwindowpid": "9",
            "windowmap": "",
            "windowactivate": "",
            "getactivewindow": "4242",
        })
        calls = []
        real = controller._run

        def run(*argv):
            calls.append(argv)
            return real(*argv)

        controller._run = run
        with mock.patch("desktop_linux_browser._proc_exe", return_value=str(spoof)), \
                mock.patch("desktop_linux_browser._proc_comm", return_value="firefox"):
            result = controller.ensure("Firefox")
        self.assertEqual(result["error"], "browser_activate_failed")
        popen.assert_not_called()
        self.assertFalse(any(cmd[1] == "windowactivate" for cmd in calls))

    def test_untrusted_path_is_not_a_browser_binary(self):
        from desktop_linux_browser import _path_is_trusted

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "firefox"
            path.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            path.chmod(0o755)
            self.assertFalse(_path_is_trusted(str(path)))

    @mock.patch.dict(os.environ, {"DISPLAY": ":98"})
    @mock.patch("desktop_linux_browser.shutil.which", return_value="/usr/bin/xdotool")
    @mock.patch("desktop_linux_browser._trusted_browser_binary", return_value="/usr/bin/firefox")
    @mock.patch("desktop_linux_browser.subprocess.Popen")
    @mock.patch("desktop_linux_browser.time.sleep")
    def test_profiles_are_per_display_and_snap_aware(self, _sleep, popen, _binary, _which):
        from desktop_linux_browser import _is_snap_firefox

        wrapper = Path(tempfile.mkdtemp()) / "firefox"
        wrapper.write_text("#!/bin/sh\nexec /snap/bin/firefox \"$@\"\n", encoding="utf-8")
        self.assertTrue(_is_snap_firefox(str(wrapper)))
        controller = LinuxBrowserController()
        controller._run = _scripted_run({"getdisplaygeometry": "10 10", "search": ""})
        home = Path(tempfile.mkdtemp())
        with mock.patch("desktop_linux_browser._trusted_browser_binary", return_value=str(wrapper)), \
                mock.patch("desktop_linux_browser.Path.home", return_value=home):
            controller.ensure("Firefox", timeout=1)
        profile = popen.call_args.args[0][3]
        self.assertIn("/snap/firefox/common/", profile)
        self.assertIn("conveyor-", profile)
        self.assertIn("_98", profile)

    def test_snap_bin_firefox_symlink_is_snap_before_realpath(self):
        import os as os_mod
        import stat as stat_mod

        from desktop_linux_browser import _is_snap_firefox, _profile_dir

        root = Path(tempfile.mkdtemp())
        usr_snap = root / "usr" / "bin" / "snap"
        usr_snap.parent.mkdir(parents=True)
        usr_snap.write_bytes(b"#!/bin/sh\n")
        link = root / "snap" / "bin" / "firefox"
        link.parent.mkdir(parents=True)
        link.symlink_to("/usr/bin/snap")
        self.assertEqual(os_mod.readlink(link), "/usr/bin/snap")
        self.assertFalse(os_mod.path.realpath(link).startswith("/snap/firefox/"))

        real_realpath = os_mod.path.realpath
        real_stat = os_mod.stat

        def realpath(path):
            if os_mod.path.abspath(path) == "/snap/bin/firefox":
                return "/usr/bin/snap"
            return real_realpath(path)

        def stat(path, *args, **kwargs):
            if os_mod.path.abspath(path) in {"/usr/bin/snap", "/snap/bin/firefox"}:
                return os_mod.stat_result((stat_mod.S_IFREG | 0o755, 0, 0, 1, 0, 0, 4096, 0, 0, 0))
            return real_stat(path, *args, **kwargs)

        with mock.patch("desktop_linux_browser.os.path.realpath", side_effect=realpath), \
                mock.patch("desktop_linux_browser.os.stat", side_effect=stat):
            self.assertTrue(_is_snap_firefox("/snap/bin/firefox"))
            self.assertEqual(os_mod.path.realpath("/snap/bin/firefox"), "/usr/bin/snap")
            home = Path(tempfile.mkdtemp())
            with mock.patch("desktop_linux_browser.Path.home", return_value=home), \
                    mock.patch.dict(os.environ, {"DISPLAY": ":98"}):
                profile = _profile_dir("Firefox", "/snap/bin/firefox")
            self.assertEqual(
                profile,
                home / "snap" / "firefox" / "common" / "conveyor-_98",
            )
            untrusted = os_mod.stat_result((stat_mod.S_IFREG | 0o755, 0, 0, 1, 1000, 1000, 4096, 0, 0, 0))
            with mock.patch("desktop_linux_browser.os.stat", return_value=untrusted):
                self.assertFalse(_is_snap_firefox("/snap/bin/firefox"))
        self.assertFalse(_is_snap_firefox("/usr/bin/snap"))


class TransportTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_step_times_out_and_cancel_is_reported(self):
        from dataclasses import replace
        from desktop_computer_loop import ComputerBackendError, HttpComputerBackend
        from desktop_computer_requests import (
            cancel_pending_computer_step,
            create_computer_step,
            create_computer_task,
        )

        with tempfile.TemporaryDirectory() as temp:
            settings = replace(_settings(Path(temp)), conveyor_computer_max_seconds=0)
            created = create_computer_task(
                settings, "Look", direct_mode=True, max_steps=2, max_seconds=10,
            )
            step = create_computer_step(settings, created["task_id"], {"action": "observe"})
            backend = HttpComputerBackend(settings, poll_interval=0.01)
            with self.assertRaises(ComputerBackendError) as caught:
                await backend.execute_step(settings, created["task_id"], step["step_id"], {"action": "observe"})
            self.assertEqual(str(caught.exception), "step_timeout")

            positive = replace(settings, conveyor_computer_max_seconds=30)
            step2 = create_computer_step(positive, created["task_id"], {"action": "observe"})
            self.assertTrue(cancel_pending_computer_step(
                positive, created["task_id"], step2["step_id"], reason="operator_stop",
            ))
            backend.settings = positive
            with self.assertRaises(ComputerBackendError) as cancelled:
                await backend.execute_step(positive, created["task_id"], step2["step_id"], {"action": "observe"})
            self.assertEqual(str(cancelled.exception), "step_cancelled")


class BrowserBoundaryTest(unittest.TestCase):
    def test_linux_nonbrowser_targets_never_reach_the_installed_app_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            transport = LocalCuaTransport("cua-driver", settings=_settings(Path(temp)))
            with mock.patch("desktop_cua.sys.platform", "linux"), \
                    mock.patch.object(transport, "_call_tool", side_effect=AssertionError("arbitrary launch")):
                for target in ("gedit", "Terminal", "Launcher", "Safari", "/tmp/firefox"):
                    self.assertEqual(transport._prepare_target_app({
                        "action": "observe", "target_app": target,
                    }), "target_app_not_found", target)
                self.assertIsNone(transport._prepare_target_app({
                    "action": "observe", "target_app": "gedit", "pid": 42,
                }))

    def test_completion_requires_the_requested_browser_and_preserves_safari(self):
        import desktop_computer_loop as loop

        for requested in ("Firefox", "Chrome", "Safari"):
            for active in ("Firefox", "Google Chrome", "Safari"):
                frame = {"screenshot_id": "fresh", "sha256": "a" * 64,
                         "active_app": active, "browser_page_state": "loaded"}
                reason = loop._reject_done(
                    goal=f"Read the webpage in {requested}", browser_goal=True,
                    observation=frame, trajectory=[{"action_type": "observe", "result_ok": True,
                                                    "screenshot_id": "fresh"}],
                    progress=loop._VisualProgress(),
                )
                matches = active == {"Chrome": "Google Chrome"}.get(requested, requested)
                self.assertEqual(reason is None, matches, (requested, active, reason))

    def test_explicit_observed_pid_keeps_its_window_binding(self):
        from desktop_computer_loop import _with_observed_target

        observed = {"pid": 42, "window_id": 7}
        self.assertEqual(_with_observed_target({"action": "type", "pid": 42}, observed)["window_id"], 7)
        self.assertNotIn("window_id", _with_observed_target({"action": "type", "pid": 99}, observed))


class BrowserInitialInputTest(unittest.IsolatedAsyncioTestCase):
    async def test_blind_browser_keystrokes_are_replaced_with_verified_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = {"result_ok": True, "action_type": "observe", "screenshot_id": "fresh",
                     "sha256": "a" * 64, "active_app": "Firefox", "pid": 42, "window_id": 7,
                     "browser_page_state": "loaded"}
            backend = _Desktop([dict(frame)])
            result = await run_computer_loop(
                _settings(Path(temp)), "Read the Firefox webpage",
                planner=_Sequence({"action": "type", "text": "http://127.0.0.1/"},
                                  {"action": "done", "summary": "read"}),
                backend=backend, max_steps=5, max_seconds=30, direct_mode=True,
            )
            self.assertEqual(result["status"], "done", result)
            self.assertEqual([a["action"] for a in backend.actions], ["observe"])
            self.assertIs(backend.actions[0]["ensure_browser"], True)
            self.assertEqual(backend.actions[0]["target_app"], "Firefox")


if __name__ == "__main__":
    unittest.main()


class BrowserSubmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_loaded_page_cannot_finish_after_url_edit_until_enter(self):
        import desktop_computer_loop as loop
        clock = {"now": 100.0}
        async def sleep(seconds):
            clock["now"] += seconds
        class Backend:
            async def execute_step(self, settings, task_id, step_id, action):
                if action["action"] != "observe":
                    return {"result_ok": True}
                return {"result_ok": True, "action_type": "observe", "sha256": "pixels",
                        "screenshot_id": "fresh", "active_app": "Firefox", "pid": 4,
                        "window_id": 8, "browser_page_state": "loaded"}
        class Planner:
            def __init__(self):
                self.actions = iter([
                    {"action": "hotkey", "keys": ["ctrl", "l"]},
                    {"action": "type", "text": "http://localhost/new-page"},
                    {"action": "done", "summary": "old page"},
                    {"action": "hotkey", "keys": ["enter"]},
                    {"action": "done", "summary": "new page verified"},
                ])
            async def next_action(self, **kwargs):
                return next(self.actions)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", lambda: clock["now"]), \
                mock.patch.object(loop, "_sleep", sleep):
            result = await run_computer_loop(
                _settings(Path(temp)), "Open the webpage in Firefox",
                planner=Planner(), backend=Backend(), max_steps=16, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["summary"], "new page verified")

    def test_submission_is_window_bound_and_draft_goals_are_exempt(self):
        from desktop_computer_loop import _BrowserSubmission
        frame = {"active_app": "Firefox", "pid": 4, "window_id": 8}
        gate = _BrowserSubmission("Open https://example.org in Firefox")
        gate.note_success({"action": "type", "text": "https://example.org"}, frame)
        self.assertTrue(gate.pending)
        self.assertEqual(gate.status(frame), "awaiting_submit")
        gate.note_success({"action": "click", "x": 1, "y": 1}, frame)
        gate.note_success({"action": "hotkey", "keys": ["enter"]}, dict(frame, window_id=9))
        self.assertTrue(gate.pending)
        gate.note_success({"action": "hotkey", "keys": ["return"]}, frame)
        self.assertFalse(gate.pending)
        draft = _BrowserSubmission("Type https://example.org in Firefox without submitting")
        draft.note_success({"action": "type", "text": "https://example.org"}, frame)
        self.assertFalse(draft.pending)
        gate = _BrowserSubmission("Read weather in Firefox")
        gate.note_success({"action": "hotkey", "keys": ["ctrl", "l"]}, frame)
        self.assertEqual(gate.status(frame), "address_focused")
        gate.note_success({"action": "type", "text": "Montreal weather"}, frame)
        self.assertTrue(gate.pending)
        self.assertNotIn("Montreal", repr(vars(gate)))
        form = _BrowserSubmission("Fill the website URL input field in Firefox")
        form.note_success({"action": "type", "text": "https://example.org"}, frame)
        self.assertFalse(form.pending)
        form.note_success({"action": "hotkey", "keys": ["ctrl", "l"]}, frame)
        form.note_success({"action": "type", "text": "https://example.org"}, frame)
        self.assertTrue(form.pending)



class LateNavigationTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_page_can_settle_after_first_deadline_without_more_input(self):
        import desktop_computer_loop as loop
        clock = {"now": 100.0}
        async def sleep(seconds):
            clock["now"] += seconds
        actions = []
        class Backend:
            async def execute_step(self, settings, task_id, step_id, action):
                actions.append(action["action"])
                if action["action"] != "observe":
                    return {"result_ok": True}
                ready = clock["now"] >= 106.0
                return {"result_ok": True, "screenshot_id": str(clock["now"]),
                        "sha256": "ready" if ready else str(clock["now"]), "active_app": "Firefox",
                        "pid": 4, "window_id": 8, "browser_page_state": "loaded" if ready else "loading"}
        class Planner:
            def __init__(self): self.submitted = False
            async def next_action(self, **kwargs):
                if not self.submitted:
                    self.submitted = True
                    return {"action": "hotkey", "keys": ["enter"]}
                return {"action": "done", "summary": "read new page"}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", lambda: clock["now"]), \
                mock.patch.object(loop, "_sleep", sleep):
            result = await run_computer_loop(
                _settings(Path(temp)), "Open the webpage in Firefox", planner=Planner(), backend=Backend(),
                max_steps=32, max_seconds=30, direct_mode=True, open_with_observe=True,
            )
        self.assertEqual(result["status"], "done")
        self.assertGreaterEqual(clock["now"], 106.3)
        self.assertEqual(actions.count("hotkey"), 1)


class DynamicPageReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_changing_loaded_frames_wait_full_budget_but_do_not_fail(self):
        import desktop_computer_loop as loop
        clock = {"now": 100.0}
        async def sleep(seconds): clock["now"] += seconds
        calls = []
        class Backend:
            async def execute_step(self, settings, task_id, step_id, action):
                calls.append(action["action"])
                if action["action"] != "observe": return {"result_ok": True}
                return {"result_ok": True, "screenshot_id": str(clock["now"]),
                        "sha256": str(clock["now"]), "active_app": "Firefox", "pid": 4,
                        "window_id": 8, "browser_page_state": "loaded"}
        class Planner:
            def __init__(self): self.submitted = False
            async def next_action(self, **kwargs):
                if not self.submitted:
                    self.submitted = True
                    return {"action": "hotkey", "keys": ["enter"]}
                return {"action": "done", "summary": "read current page"}
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(loop, "_monotonic", lambda: clock["now"]), \
                mock.patch.object(loop, "_sleep", sleep):
            result = await run_computer_loop(
                _settings(Path(temp)), "Read the weather webpage in Firefox",
                planner=Planner(), backend=Backend(), max_steps=32, max_seconds=30,
                direct_mode=True, open_with_observe=True,
            )
        self.assertEqual(result["status"], "done", result)
        self.assertGreaterEqual(clock["now"], 105.0)
        self.assertEqual(calls.count("hotkey"), 1)

    def test_dynamic_readiness_rejects_loading_missing_images_and_changed_focus(self):
        import desktop_computer_loop as loop
        ready = {"screenshot_id": "fresh", "sha256": "a", "active_app": "Firefox",
                 "pid": 4, "window_id": 8, "browser_page_state": "loaded"}
        for bad in (dict(ready, browser_page_state="loading"),
                    dict(ready, browser_page_state="error"), dict(ready, effect="loading"),
                    dict(ready, screenshot_id=""),
                    dict(ready, sha256={"bad": "digest"}),
                    dict(ready, active_app="Terminal"), dict(ready, window_id=9)):
            gate = loop._NavigationSettle(100.0)
            gate.sample(ready, 104.5)
            gate.sample(bad, 104.9)
            self.assertFalse(gate.loaded_after_deadline(105.0), bad)
        gate = loop._NavigationSettle(100.0)
        gate.sample(ready, 104.5)
        gate.sample(dict(ready, sha256="b"), 104.9)
        self.assertFalse(gate.loaded_after_deadline(104.99))
        self.assertTrue(gate.loaded_after_deadline(105.0))
        self.assertFalse(gate.loaded_after_deadline(106.0))
