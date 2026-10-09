"""Isolated Xvfb check for the host browser controller.

Skipped unless Xvfb and xdotool are installed. The server is a private
display started by this test; it never attaches to the user's session and
never falls back to the host desktop. It does not launch a real browser
and does not treat a process start as a loaded page.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from desktop_linux_browser import LinuxBrowserController


def _tools() -> bool:
    return bool(shutil.which("Xvfb") and shutil.which("xdotool"))


@unittest.skipUnless(_tools(), "Xvfb and xdotool are required")
class IsolatedX11BrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.display = f":{200 + (os.getpid() % 200)}"
        self.proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", "640x480x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop)
        deadline = time.monotonic() + 3
        ready = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.fail("Xvfb exited before the display was ready")
            env = {key: value for key, value in os.environ.items() if key != "XAUTHORITY"}
            env["DISPLAY"] = self.display
            probe = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                env=env, capture_output=True, text=True, check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                ready = True
                break
            time.sleep(0.05)
        if not ready:
            self.fail("private X display did not answer")
        self._old = {key: os.environ.get(key) for key in ("DISPLAY", "XAUTHORITY")}
        os.environ["DISPLAY"] = self.display
        os.environ.pop("XAUTHORITY", None)

    def tearDown(self) -> None:
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def test_private_display_answers_and_a_blocked_browser_is_not_started(self) -> None:
        controller = LinuxBrowserController()
        self.assertIsNone(controller._display_error())
        with mock.patch("desktop_linux_browser.subprocess.Popen") as popen:
            result = controller.ensure("Firefox", blocked_apps=("Firefox",))
        self.assertEqual(result["error"], "browser_disallowed")
        popen.assert_not_called()

    def test_bad_xauthority_is_unreachable_before_launch(self) -> None:
        os.environ["XAUTHORITY"] = str(Path("/tmp") / "conveyor-missing-xauth")
        controller = LinuxBrowserController()
        with mock.patch("desktop_linux_browser.subprocess.Popen") as popen:
            result = controller.ensure("Firefox")
        self.assertEqual(result["error"], "browser_display_unreachable")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
