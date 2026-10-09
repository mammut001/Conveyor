"""Restricted X11 browser lifecycle for the shared Linux desktop.

This is *not* a general process launcher. Only known browser executables on
trusted, root-owned system paths can be started, without a shell, on the
already configured DISPLAY. X11 window identity and focus are checked
against the real process, not WM_CLASS alone.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path


BROWSERS = {
    "Firefox": (("firefox", "firefox-esr", "firefox-bin"), r"firefox|Navigator"),
    "Chromium": (("chromium", "chromium-browser"), r"chromium|Chromium"),
    "Google Chrome": (
        ("google-chrome", "google-chrome-stable", "chrome"),
        r"google-chrome|Google-chrome|Chrome",
    ),
}
# Exact executable basenames only. Substrings are never enough: "chrome"
# must not accept "chrome-helper" or "notchrome".
_PROCESS_BASENAMES = {
    "firefox": "Firefox",
    "firefox-esr": "Firefox",
    "firefox-bin": "Firefox",
    "chromium": "Chromium",
    "chromium-browser": "Chromium",
    "chrome": "Google Chrome",
    "google-chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
}
ALIASES = {
    "firefox": "Firefox", "firefox-esr": "Firefox", "firefox-bin": "Firefox",
    "navigator": "Firefox",
    "chromium": "Chromium", "chromium-browser": "Chromium",
    "google chrome": "Google Chrome", "google-chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
    "chrome": "Google Chrome", "browser": "Browser",
}
_TRUSTED_ROOTS = (
    "/usr/bin/",
    "/usr/lib/",
    "/usr/lib64/",
    "/snap/bin/",
    "/snap/firefox/",
    "/opt/google/chrome/",
    "/opt/chromium/",
    "/usr/lib/firefox/",
    "/usr/lib/chromium/",
    "/usr/lib/chromium-browser/",
)
_SEARCH_DIRS = (
    "/usr/bin",
    "/usr/lib/firefox",
    "/usr/lib/chromium",
    "/usr/lib/chromium-browser",
    "/snap/bin",
    "/opt/google/chrome",
    "/opt/chromium",
)


def canonical_browser(value: object) -> str | None:
    return ALIASES.get(str(value or "").strip().lower())


def canonical_app(value: object) -> str:
    raw = str(value or "").strip()
    return canonical_browser(raw) or raw or "Unknown"


# comm is at most 15 bytes. These prefixes are unique among terminal names.
_TERMINAL_NAMES = {
    "gnome-terminal": "Terminal",
    "gnome-terminal-server": "Terminal",
    "konsole": "Terminal",
    "xterm": "Terminal",
    "xfce4-terminal": "Terminal",
    "alacritty": "Terminal",
    "kitty": "Terminal",
    "tilix": "Terminal",
    "terminator": "Terminal",
    "kgx": "Terminal",
    "ptyxis": "Terminal",
    "wezterm": "Terminal",
    "wezterm-gui": "Terminal",
    "foot": "Terminal",
    "urxvt": "Terminal",
    "lxterminal": "Terminal",
    "qterminal": "Terminal",
    "mate-terminal": "Terminal",
    "gnome-terminal-": "Terminal",
}


def app_from_process_names(exe_base: str | None, comm: str | None) -> str:
    """Map a process to a browser using exact names only.

    ``/proc/<pid>/comm`` is truncated to 15 bytes. A truncated name counts
    only when it is a unique prefix of one known basename longer than that.
    """
    base = str(exe_base or "").strip()
    if base:
        return _PROCESS_BASENAMES.get(base, "Unknown")
    text = str(comm or "").strip()
    if text in _PROCESS_BASENAMES:
        return _PROCESS_BASENAMES[text]
    if len(text) == 15:
        matches = [
            name for name in _PROCESS_BASENAMES
            if len(name) > 15 and name.startswith(text)
        ]
        if len(matches) == 1:
            return _PROCESS_BASENAMES[matches[0]]
    return "Unknown"


def _terminal_name(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in _TERMINAL_NAMES:
        return "Terminal"
    if len(text) == 15:
        matches = [
            name for name in _TERMINAL_NAMES
            if len(name) > 15 and name.startswith(text)
        ]
        if len(matches) == 1:
            return "Terminal"
    return None


def _proc_exe(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except (OSError, ValueError):
        return None


def _proc_comm(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return text[:64] or None


def linux_process_app(pid: int) -> str:
    """Attribute a PID without trusting a user-owned browser basename.

    A readable ``/proc/<pid>/exe`` is the source of truth. Browser names
    require a root-owned executable under a system directory. When that
    path is readable but untrusted, ``comm`` is not allowed to promote the
    process to Firefox or Chrome. Other basenames stay available so
    allow and block lists can still name the real program, and common
    terminal binaries map to Terminal (the default blocklist entry).
    """
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return "Unknown"
    if pid_i <= 0:
        return "Unknown"
    exe = _proc_exe(pid_i)
    if exe:
        base = os.path.basename(exe)
        terminal = _terminal_name(base)
        if terminal:
            return terminal
        if base in _PROCESS_BASENAMES:
            if _path_is_trusted(exe):
                return _PROCESS_BASENAMES[base]
            return "Unknown"
        return base or "Unknown"
    comm = _proc_comm(pid_i) or ""
    browser = app_from_process_names(None, comm)
    if browser != "Unknown":
        return browser
    return _terminal_name(comm) or comm or "Unknown"


def _path_is_trusted(path: str) -> bool:
    """True for a root-owned regular file under a system browser directory."""
    try:
        target = os.path.realpath(path)
        st = os.stat(target)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != 0 or (st.st_mode & 0o022):
        return False
    return any(target.startswith(root) for root in _TRUSTED_ROOTS)


def _trusted_browser_binary(browser: str) -> str | None:
    """Resolve a browser without consulting PATH (PATH is attacker-controlled)."""
    for directory in _SEARCH_DIRS:
        for name in BROWSERS[browser][0]:
            candidate = os.path.join(directory, name)
            if _path_is_trusted(candidate):
                return candidate
    return None


def _is_known_snap_launcher(path: str) -> bool:
    """True for the Snap Firefox launcher before symlink resolution.

    On Ubuntu ``/snap/bin/firefox`` is a symlink to ``/usr/bin/snap``.
    ``realpath`` therefore leaves ``/snap/`` and cannot be the only check.
    """
    try:
        original = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    return original == "/snap/bin/firefox" or original.startswith("/snap/firefox/")


def _is_snap_firefox(binary: str) -> bool:
    # The launcher path is decided before realpath. Trust still applies:
    # a known Snap path must resolve to a root-owned system file
    # (the ``/usr/bin/snap`` multiplexer qualifies).
    if _is_known_snap_launcher(binary):
        return _path_is_trusted(binary)
    real = os.path.realpath(binary)
    if real.startswith("/snap/") or "/snap/firefox/" in real:
        return True
    try:
        with open(binary, "rb") as handle:
            head = handle.read(4096).decode("utf-8", errors="ignore")
    except OSError:
        return False
    if not head.startswith("#!"):
        return False
    return "/snap/bin/firefox" in head or "snap run firefox" in head or "snap run --firefox" in head


def _display_slug() -> str:
    raw = os.environ.get("DISPLAY") or "nodisplay"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:40] or "nodisplay"


def _profile_dir(browser: str, binary: str) -> Path:
    """One profile per DISPLAY so two X servers do not share a Firefox lock."""
    slug = _display_slug()
    if browser == "Firefox" and _is_snap_firefox(binary):
        return Path.home() / "snap" / "firefox" / "common" / f"conveyor-{slug}"
    return (
        Path.home() / ".local" / "share" / "conveyor" / "host-browser"
        / slug / browser.replace(" ", "-").lower()
    )


def _class_matches(browser: str, class_name: str) -> bool:
    text = (class_name or "").strip()
    if not text or len(text) > 80:
        return False
    return re.fullmatch(BROWSERS[browser][1], text, flags=re.IGNORECASE) is not None


_WM_CLASS_RE = re.compile(
    r'WM_CLASS\(STRING\)\s*=\s*"((?:\\.|[^"\\])*)"\s*,\s*"((?:\\.|[^"\\])*)"'
)
# Snap Firefox reports this pair. It is not a substring match on "firefox".
_SNAP_FIREFOX_CLASS = ("Firefox", "firefox_firefox")


def parse_wm_class(text: str) -> tuple[str, str] | None:
    """Parse ``xprop WM_CLASS`` output into ``(instance, class)``."""
    match = _WM_CLASS_RE.search(text or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def wm_class_matches(browser: str, instance: str, klass: str) -> bool:
    """Exact WM_CLASS pair for one allow-listed browser.

    Snap Firefox is ``"Firefox", "firefox_firefox"``. Other builds match the
    existing class or instance token exactly (``firefox``, ``Navigator``, …).
    """
    inst = (instance or "").strip()
    kind = (klass or "").strip()
    if browser == "Firefox" and (inst, kind) == _SNAP_FIREFOX_CLASS:
        return True
    if kind == "firefox_firefox" or inst == "firefox_firefox":
        return False
    return _class_matches(browser, kind) or _class_matches(browser, inst)


def xprop_browser_target(text: str, browser: str | None = None) -> bool:
    """True for a managed normal or minimized browser window.

    Hidden Firefox helpers omit ``WM_STATE``. The profile-lock dialog is
    ``_NET_WM_WINDOW_TYPE_DIALOG`` (title "Close Firefox"). Neither is a
    typing target. Iconic (minimized) normal windows still count.
    """
    parsed = parse_wm_class(text or "")
    if parsed is None:
        return False
    instance, klass = parsed
    if browser is None:
        matched = any(wm_class_matches(name, instance, klass) for name in BROWSERS)
    else:
        matched = wm_class_matches(browser, instance, klass)
    if not matched:
        return False
    state = re.search(r"window state:\s*([A-Za-z]+)", text or "")
    if state is None or state.group(1) not in {"Normal", "Iconic"}:
        return False
    types = re.findall(r"_NET_WM_WINDOW_TYPE_[A-Z0-9_]+", text or "")
    return types == ["_NET_WM_WINDOW_TYPE_NORMAL"]


class LinuxBrowserController:
    """Deterministic, allow-listed browser activation on a single X display."""

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(argv, 1, "", "")

    def _display_error(self) -> str | None:
        if not os.environ.get("DISPLAY"):
            return "browser_display_missing"
        xauth = os.environ.get("XAUTHORITY")
        if xauth and not os.path.isfile(xauth):
            return "browser_display_unreachable"
        probe = self._run("xdotool", "getdisplaygeometry")
        parts = (probe.stdout or "").split()
        if probe.returncode != 0 or len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return "browser_display_unreachable"
        return None

    def _xprop(self, window: str) -> str:
        # One supported xprop call. A missing property makes xprop exit 1
        # while still printing the properties that exist; parse stdout either way.
        result = self._run(
            "xprop", "-id", window, "WM_CLASS", "WM_STATE", "_NET_WM_WINDOW_TYPE",
        )
        return result.stdout or ""

    def _window_ids(self, browser: str) -> list[str]:
        # Include minimized/occluded windows. --onlyvisible misses a live
        # browser and the next step launches a duplicate. Search hits helper
        # and dialog windows too; those are dropped after xprop.
        regex = BROWSERS[browser][1]
        result = self._run("xdotool", "search", "--class", regex)
        if result.returncode and not (result.stdout or "").strip():
            return []
        ids = [line.strip() for line in (result.stdout or "").splitlines()]
        accepted: list[str] = []
        for wid in ids:
            if not re.fullmatch(r"\d{1,12}", wid):
                continue
            if xprop_browser_target(self._xprop(wid), browser):
                accepted.append(wid)
        return accepted

    def _window_pid(self, window: str) -> int | None:
        result = self._run("xdotool", "getwindowpid", window)
        text = (result.stdout or "").strip()
        if result.returncode or not text.isdigit():
            return None
        value = int(text)
        return value if value > 0 else None

    def _trusted_window_pid(self, window: str, browser: str) -> int | None:
        """Class and process must both match before any focus change.

        WM_CLASS comes from xprop. Snap Firefox's class is ``firefox_firefox``
        and still requires a trusted browser PID.
        """
        if not xprop_browser_target(self._xprop(window), browser):
            return None
        pid = self._window_pid(window)
        if pid is None or canonical_browser(linux_process_app(pid)) != browser:
            return None
        return pid

    def _focus(self, window: str, browser: str) -> int | None:
        pid = self._trusted_window_pid(window, browser)
        if pid is None:
            return None
        # Map first so a minimized browser is a real foreground target.
        self._run("xdotool", "windowmap", window)
        if self._run("xdotool", "windowactivate", "--sync", window).returncode:
            return None
        active = self._run("xdotool", "getactivewindow")
        current = (active.stdout or "").strip()
        if active.returncode or current != window:
            return None
        foreground = self._window_pid(current)
        if foreground != pid or canonical_browser(linux_process_app(foreground or 0)) != browser:
            return None
        return foreground

    def active_app(self) -> str:
        """Foreground app from the window PID, never from WM_CLASS alone."""
        active = self._run("xdotool", "getactivewindow")
        wid = (active.stdout or "").strip()
        if active.returncode or not re.fullmatch(r"\d{1,12}", wid):
            return "Unknown"
        pid = self._window_pid(wid)
        if pid is None:
            return "Unknown"
        return linux_process_app(pid)

    def active_window_title(self) -> str:
        """Capped title of the active X window. Empty when the query fails."""
        if not os.environ.get("DISPLAY"):
            return ""
        active = self._run("xdotool", "getactivewindow")
        wid = (active.stdout or "").strip()
        if active.returncode or not re.fullmatch(r"\d{1,12}", wid):
            return ""
        named = self._run("xdotool", "getwindowname", wid)
        title = (named.stdout or "").strip()
        if named.returncode or not title:
            return ""
        return title[:120]

    def keyboard_target_ready(self, pid: object, window_id: object) -> bool:
        """True when the intended window is still mapped and in front.

        Typing after focus has moved would land in whichever shell window
        is now foreground (often the launcher or a panel).
        """
        active = self._run("xdotool", "getactivewindow")
        wid = (active.stdout or "").strip()
        if active.returncode or not re.fullmatch(r"\d{1,12}", wid):
            return False
        if window_id is not None:
            try:
                if int(window_id) != int(wid):
                    return False
            except (TypeError, ValueError):
                return False
        got = self._window_pid(wid)
        if got is None:
            return False
        if pid is not None:
            try:
                if int(pid) != got:
                    return False
            except (TypeError, ValueError):
                return False
        geo = self._run("xdotool", "getwindowgeometry", wid)
        return geo.returncode == 0

    def _binary(self, browser: str) -> str | None:
        return _trusted_browser_binary(browser)

    def ensure(
        self, name: str, *,
        allowed_apps: tuple[str, ...] = (),
        blocked_apps: tuple[str, ...] = (),
        timeout: float = 15.0,
        expected_pid: int | None = None,
        expected_window: int | None = None,
    ) -> dict:
        """Return a verified foreground browser PID or a stable error code."""
        browser = canonical_browser(name)
        if browser is None:
            return {"ok": False, "error": "browser_not_supported"}
        if not shutil.which("xdotool"):
            return {"ok": False, "error": "xdotool_missing"}

        allowed = {canonical_app(a).lower() for a in allowed_apps}
        blocked = {canonical_app(a).lower() for a in blocked_apps}
        candidates = list(BROWSERS) if browser == "Browser" else [browser]
        if browser == "Browser":
            # active_app hits the display; skip it until policy allows a browser.
            pass
        candidates = [
            item for item in candidates if item.lower() not in blocked
            and (not allowed or item.lower() in allowed)
        ]
        if not candidates:
            return {"ok": False, "error": "browser_disallowed"}

        display_error = self._display_error()
        if display_error:
            return {"ok": False, "error": display_error}

        if browser == "Browser":
            active = canonical_browser(self.active_app())
            if active in candidates:
                candidates.remove(active)
                candidates.insert(0, active)

        # An explicit pid/window that is not this browser must not be replaced
        # by focusing or launching some other window.
        constrained = expected_pid is not None or expected_window is not None
        seen_window = False
        for item in candidates:
            for wid in self._window_ids(item):
                if expected_window is not None and int(wid) != int(expected_window):
                    continue
                seen_window = True
                if expected_pid is not None:
                    pre = self._trusted_window_pid(wid, item)
                    if pre != int(expected_pid):
                        continue
                pid = self._focus(wid, item)
                if pid and (expected_pid is None or pid == int(expected_pid)):
                    return {"ok": True, "name": item, "pid": pid, "window_id": int(wid)}
        if seen_window or constrained:
            return {"ok": False, "error": "target_identity_conflict" if constrained else "browser_activate_failed"}

        selected = None
        for candidate in candidates:
            binary = self._binary(candidate)
            if binary:
                selected = (candidate, binary)
                break
        if selected is None:
            return {"ok": False, "error": "browser_binary_missing"}
        item, binary = selected
        profile = _profile_dir(item, binary)
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
        saw_unverified = False
        while time.monotonic() < deadline:
            for wid in self._window_ids(item):
                saw_unverified = True
                pid = self._focus(wid, item)
                if pid:
                    return {"ok": True, "name": item, "pid": pid, "window_id": int(wid)}
            time.sleep(0.5)
        if saw_unverified:
            return {"ok": False, "error": "browser_activate_failed"}
        return {"ok": False, "error": "browser_window_not_mapped"}
