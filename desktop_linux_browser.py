"""Restricted X11 browser lifecycle for the shared Linux desktop.

This is *not* a general process launcher. Only known browser executables can
be started, without a shell, on the already configured DISPLAY. X11 window
identity and focus are checked independently of cua-driver's app enumeration.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path


BROWSERS = {
    "Firefox": (("firefox", "firefox-esr", "/snap/bin/firefox"), r"firefox|Navigator"),
    "Chromium": (("chromium", "chromium-browser"), r"chromium|Chromium"),
    "Google Chrome": (("google-chrome", "google-chrome-stable"), r"google-chrome|Google-chrome"),
}
ALIASES = {
    "firefox": "Firefox", "firefox-esr": "Firefox", "navigator": "Firefox",
    "chromium": "Chromium", "chromium-browser": "Chromium",
    "google chrome": "Google Chrome", "google-chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
    "chrome": "Google Chrome", "browser": "Browser",
}


def canonical_browser(value: object) -> str | None:
    return ALIASES.get(str(value or "").strip().lower())


def canonical_app(value: object) -> str:
    raw = str(value or "").strip()
    return canonical_browser(raw) or raw or "Unknown"


def linux_process_app(pid: int) -> str:
    """Best-effort local PID attribution; never fall back to another app."""
    try:
        comm = Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip()
    except (OSError, ValueError, TypeError):
        return "Unknown"
    return canonical_app(comm)


class LinuxBrowserController:
    """Deterministic, allow-listed browser activation on a single X display."""

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(argv, 1, "", "")

    def _window_ids(self, browser: str) -> list[str]:
        regex = BROWSERS[browser][1]
        result = self._run("xdotool", "search", "--onlyvisible", "--class", regex)
        if result.returncode:
            return []
        ids = [line.strip() for line in result.stdout.splitlines()]
        return [wid for wid in ids if re.fullmatch(r"\d{1,12}", wid)]

    def _focus(self, window: str, browser: str) -> int | None:
        if self._run("xdotool", "windowactivate", "--sync", window).returncode:
            return None
        active = self._run("xdotool", "getactivewindow")
        if active.returncode or active.stdout.strip() != window:
            return None
        pid = self._run("xdotool", "getwindowpid", window)
        if pid.returncode or not pid.stdout.strip().isdigit():
            return None
        value = int(pid.stdout.strip())
        # WM_CLASS alone is controlled by the window owner. Check the real
        # process too, or an unrelated launcher window could be trusted.
        if value <= 0 or canonical_browser(linux_process_app(value)) != browser:
            return None
        return value

    def active_app(self) -> str:
        result = self._run("xdotool", "getactivewindow", "getwindowclassname")
        return canonical_app(result.stdout) if result.returncode == 0 else "Unknown"

    def _binary(self, browser: str) -> str | None:
        for candidate in BROWSERS[browser][0]:
            path = shutil.which(candidate)
            if path:
                return path
        return None

    def ensure(
        self, name: str, *,
        allowed_apps: tuple[str, ...] = (),
        blocked_apps: tuple[str, ...] = (),
        timeout: float = 15.0,
    ) -> dict:
        """Return a verified foreground browser PID or a stable error code."""
        browser = canonical_browser(name)
        if browser is None:
            return {"ok": False, "error": "browser_not_supported"}
        if not os.environ.get("DISPLAY"):
            return {"ok": False, "error": "browser_display_missing"}
        if not shutil.which("xdotool"):
            return {"ok": False, "error": "xdotool_missing"}

        allowed = {canonical_app(a).lower() for a in allowed_apps}
        blocked = {canonical_app(a).lower() for a in blocked_apps}
        candidates = list(BROWSERS) if browser == "Browser" else [browser]
        # Generic "current browser" should keep the current browser first.
        if browser == "Browser":
            active = canonical_browser(self.active_app())
            if active in candidates:
                candidates.remove(active)
                candidates.insert(0, active)
        candidates = [
            item for item in candidates if item.lower() not in blocked
            and (not allowed or item.lower() in allowed)
        ]
        if not candidates:
            return {"ok": False, "error": "browser_disallowed"}

        # Prefer an existing mapped browser window; launching a second copy
        # just because a stale app list omitted the first is not recovery.
        seen_window = False
        for item in candidates:
            for wid in self._window_ids(item):
                seen_window = True
                pid = self._focus(wid, item)
                if pid:
                    return {"ok": True, "name": item, "pid": pid, "window_id": int(wid)}
        if seen_window:
            return {"ok": False, "error": "browser_activate_failed"}

        selected = None
        for candidate in candidates:
            binary = self._binary(candidate)
            if binary:
                selected = (candidate, binary)
                break
        if selected is None:
            return {"ok": False, "error": "browser_binary_missing"}
        item, binary = selected
        # A dedicated profile prevents Firefox redirecting to a browser
        # already running on a different DISPLAY for the same Linux user.
        if item == "Firefox" and binary.startswith("/snap/"):
            profile = Path.home() / "snap" / "firefox" / "common" / "conveyor-host-browser"
        else:
            profile = Path.home() / ".local" / "share" / "conveyor" / "host-browser" / item.replace(" ", "-").lower()
        try:
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            if item == "Firefox":
                command = [binary, "--no-remote", "--profile", str(profile), "--new-window", "about:blank"]
            else:
                command = [binary, f"--user-data-dir={profile}", "--new-window", "about:blank"]
            # Never a shell or a model-supplied command/URL.
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            return {"ok": False, "error": "browser_launch_failed"}

        deadline = time.monotonic() + max(1.0, min(timeout, 30.0))
        while time.monotonic() < deadline:
            for wid in self._window_ids(item):
                pid = self._focus(wid)
                if pid:
                    return {"ok": True, "name": item, "pid": pid, "window_id": int(wid)}
            time.sleep(0.5)
        return {"ok": False, "error": "browser_window_not_mapped"}
