"""target_app is checked against the app allow/block lists before it is launched or focused."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from conveyor.desktop.desktop_cua import LocalCuaTransport
from conveyor.desktop.desktop_x11 import X11Desktop
from tests.test_computer_windows import _settings

_PROBE_OK = {"available": True, "path": "/usr/local/bin/cua-driver", "error": None}


class _Mac:
    """A LocalCuaTransport on macOS whose driver calls are recorded."""

    def __init__(self, case: unittest.TestCase, **settings: Any) -> None:
        root = Path(tempfile.mkdtemp())
        self.transport = LocalCuaTransport("cua-driver call", settings=_settings(root, **settings))
        self.calls: list[str] = []
        self.observed = 0

        def fake_call(name, args=None, timeout=None):
            self.calls.append(name)
            if name == "list_apps":
                return {"ok": True, "data": {"apps": [
                    {"name": "Calculator", "running": False, "launch_path": "/System/Applications/Calculator.app"},
                    {"name": "Messages", "running": False, "launch_path": "/System/Applications/Messages.app"},
                ]}}
            if name == "launch_app":
                return {"ok": True, "data": {"pid": 99}}
            if name == "bring_to_front":
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected_tool"}

        def fake_observe(action, node_id):
            self.observed += 1
            return {"result_ok": True, "action_type": "observe", "node_id": node_id}

        self.transport._call_tool = fake_call  # type: ignore[method-assign]
        self.transport._observe = fake_observe  # type: ignore[method-assign]
        for patcher in (
            mock.patch("conveyor.desktop.desktop_cua.sys.platform", "darwin"),
            mock.patch("conveyor.desktop.desktop_cua.probe_cua_driver", return_value=_PROBE_OK),
            mock.patch("conveyor.desktop.desktop_cua.get_active_app_macos", return_value="Codex"),
            mock.patch("conveyor.desktop.desktop_cua.get_app_name_for_pid", return_value="Calculator"),
        ):
            patcher.start()
            case.addCleanup(patcher.stop)

    @property
    def side_effects(self) -> list[str]:
        return [name for name in self.calls if name in {"launch_app", "bring_to_front", "list_apps"}]


class MacTargetAppPolicyTest(unittest.TestCase):
    def test_observe_outside_allowlist_is_not_launched(self) -> None:
        mac = _Mac(self, conveyor_computer_allowed_apps=("Calculator",))
        result = mac.transport.execute({"action": "observe", "target_app": "Messages"}, "node-1")
        self.assertFalse(result["result_ok"])
        self.assertEqual(result["error"], "app_not_in_allowlist:Messages")
        self.assertEqual(mac.side_effects, [])
        self.assertEqual(mac.observed, 0)

    def test_blocked_target_is_not_launched_for_any_action(self) -> None:
        for action in (
            {"action": "observe", "target_app": "Messages"},
            {"action": "wait", "seconds": 0, "target_app": "Messages"},
            {"action": "click", "element_index": 3, "target_app": "messages"},
            {"action": "type", "text": "hi", "target_app": "Messages"},
        ):
            mac = _Mac(self, conveyor_computer_blocked_apps=("Messages",))
            result = mac.transport.execute(dict(action), "node-1")
            self.assertFalse(result["result_ok"], action)
            self.assertTrue(str(result["error"]).startswith("blocked_app:"), result)
            self.assertEqual(mac.side_effects, [], action)

    def test_non_string_target_is_refused(self) -> None:
        mac = _Mac(self)
        result = mac.transport.execute({"action": "observe", "target_app": ["Messages"]}, "node-1")
        self.assertEqual(result["error"], "invalid_target_app")
        self.assertEqual(mac.side_effects, [])

    def test_allowlisted_target_is_launched_and_observed(self) -> None:
        mac = _Mac(self, conveyor_computer_allowed_apps=("Calculator",))
        result = mac.transport.execute({"action": "observe", "target_app": "Calculator"}, "node-1")
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(mac.calls, ["list_apps", "launch_app", "bring_to_front"])
        self.assertEqual(mac.observed, 1)
        self.assertEqual(result["active_app"], "Calculator")

    def test_observe_without_target_keeps_blocklist_only_behaviour(self) -> None:
        mac = _Mac(self, conveyor_computer_allowed_apps=("Calculator",))
        result = mac.transport.execute({"action": "observe"}, "node-1")
        self.assertTrue(result["result_ok"], result)
        self.assertEqual(result["active_app"], "Codex")
        self.assertEqual(mac.side_effects, [])

        blocked = _Mac(self, conveyor_computer_blocked_apps=("Codex",))
        result = blocked.transport.execute({"action": "observe"}, "node-1")
        self.assertEqual(result["error"], "blocked_app:Codex")


class LinuxTargetAppPolicyTest(unittest.TestCase):
    def _transport(self, **settings: Any) -> LocalCuaTransport:
        transport = LocalCuaTransport("cua-driver call", settings=_settings(Path(tempfile.mkdtemp()), **settings))
        transport._call_tool = mock.Mock(side_effect=AssertionError("no driver call expected"))  # type: ignore[method-assign]
        return transport

    def _run(self, transport: LocalCuaTransport, action: dict) -> tuple[dict, mock.Mock]:
        ensure = mock.Mock(return_value={"ok": True, "pid": 41, "window_id": 42, "name": "Firefox"})
        with mock.patch("conveyor.desktop.desktop_cua.sys.platform", "linux"), \
                mock.patch("conveyor.desktop.desktop_cua.probe_cua_driver", return_value=_PROBE_OK), \
                mock.patch("conveyor.desktop.desktop_linux_browser.LinuxBrowserController.ensure", ensure), \
                mock.patch("conveyor.desktop.desktop_cua.get_app_name_for_pid", return_value="Firefox"), \
                mock.patch.object(transport, "_observe", return_value={"result_ok": True}):
            return transport.execute(action, "node-1"), ensure

    def test_browser_outside_allowlist_is_not_focused(self) -> None:
        transport = self._transport(conveyor_computer_allowed_apps=("Calculator",))
        result, ensure = self._run(transport, {"action": "observe", "target_app": "Firefox"})
        self.assertEqual(result["error"], "browser_disallowed")
        ensure.assert_not_called()

    def test_blocked_browser_is_not_focused(self) -> None:
        transport = self._transport(conveyor_computer_blocked_apps=("Firefox",))
        result, ensure = self._run(transport, {"action": "click", "element_index": 1, "target_app": "firefox"})
        self.assertEqual(result["error"], "browser_disallowed")
        ensure.assert_not_called()

    def test_allowlisted_browser_is_focused(self) -> None:
        transport = self._transport(conveyor_computer_allowed_apps=("Firefox",))
        result, ensure = self._run(transport, {"action": "observe", "target_app": "Firefox"})
        self.assertTrue(result["result_ok"], result)
        ensure.assert_called_once()


class X11TargetAppPolicyTest(unittest.TestCase):
    def _desktop(self, **settings: Any) -> X11Desktop:
        return X11Desktop(_settings(Path(tempfile.mkdtemp()), **settings), agent_id="a1", display=5)

    def test_blocked_browser_target_is_not_focused(self) -> None:
        desktop = self._desktop(conveyor_computer_blocked_apps=("Firefox",))
        with mock.patch("conveyor.desktop.desktop_linux_browser.LinuxBrowserController.ensure") as ensure, \
                mock.patch.object(desktop, "observe") as observe:
            result = desktop.execute({"action": "observe", "target_app": "Firefox"})
        self.assertEqual(result["error"], "browser_disallowed")
        ensure.assert_not_called()
        observe.assert_not_called()

    def test_browser_outside_allowlist_is_not_focused(self) -> None:
        desktop = self._desktop(conveyor_computer_allowed_apps=("Google Chrome",))
        with mock.patch("conveyor.desktop.desktop_linux_browser.LinuxBrowserController.ensure") as ensure:
            result = desktop.execute({"action": "observe", "target_app": "Firefox"})
        self.assertEqual(result["error"], "browser_disallowed")
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
