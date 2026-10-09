"""desktop_x11.py — computer use on an agent's own X display.

The host desktop is driven through cua-driver and kernel uinput devices,
which are not tied to a display: every X server on the machine would receive
the same clicks. An agent's desktop is therefore driven with XTEST instead
(``xdotool``) and captured with ``import``, both addressed by ``DISPLAY`` —
the same mechanism the live screen uses for a human.

The contract with the planner is deliberately simpler than the host's:

- an observation is one screenshot of the whole screen;
- click coordinates are pixels in that screenshot;
- type, hotkey and scroll go to whatever has focus (click first).

Steps are claimed and completed in the shared request store, so the trail,
the redaction rules and the takeover checks are the ones every task gets.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import agents
from config import Settings
from desktop_computer_requests import claim_computer_step, complete_computer_step, x11_node_id

logger = logging.getLogger("conveyor.desktop_x11")

MAX_WAIT_SECONDS = 10.0
BROWSER_WAIT_SECONDS = 40.0
TYPE_CHUNK = 400
# X window classes of the browsers an agent desktop may run (regex).
BROWSER_CLASSES = "firefox|Navigator|chromium|chrome"
# A planner used to macOS says "cmd"; on this desktop the shortcut key is ctrl.
MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl", "cmd": "ctrl", "command": "ctrl", "meta": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift", "super": "super", "win": "super",
}
KEYS = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "space": "space", "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next", "page_up": "Prior", "page_down": "Next",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}


class X11Error(Exception):
    """A step that could not be carried out; the text is a short machine code."""


def _key_name(key: str) -> str | None:
    lowered = key.strip().lower()
    if lowered in KEYS:
        return KEYS[lowered]
    if len(key) == 1:
        return key if key.isascii() and key.isalnum() else f"U{ord(key):04X}"
    return None


def hotkey_argument(keys: Any) -> str:
    """``["cmd", "l"]`` → ``"ctrl+l"`` for ``xdotool key``."""
    if not isinstance(keys, list) or not keys or len(keys) > 5:
        raise X11Error("bad_hotkey")
    parts: list[str] = []
    for index, raw in enumerate(keys):
        name = str(raw)
        modifier = MODIFIERS.get(name.strip().lower())
        if modifier and index < len(keys) - 1:
            parts.append(modifier)
            continue
        key = _key_name(name) or modifier
        if key is None:
            raise X11Error("bad_hotkey")
        parts.append(key)
    return "+".join(parts)


class X11Desktop:
    """Blocking operations on one X display."""

    def __init__(self, settings: Settings, *, agent_id: str, display: int) -> None:
        self.settings = settings
        self.agent_id = agent_id
        self.display = display
        self.env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "DISPLAY": f":{display}",
            "XAUTHORITY": str(agents.client_xauthority_path()),
        }

    def _run(self, *command: str, timeout: float = 15.0, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(command), env=self.env, timeout=timeout, check=False, text=text,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def _xdotool(self, *args: str) -> None:
        if not shutil.which("xdotool"):
            raise X11Error("xdotool_missing")
        if self._run("xdotool", *args).returncode != 0:
            raise X11Error("input_failed")

    def geometry(self) -> tuple[int, int]:
        try:
            out = self._run("xdotool", "getdisplaygeometry").stdout.split()
            return int(out[0]), int(out[1])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            raise X11Error("desktop_unreachable") from None

    def _point(self, action: dict) -> tuple[str, str]:
        try:
            x, y = int(round(float(action["x"]))), int(round(float(action["y"])))
        except (KeyError, TypeError, ValueError):
            raise X11Error("click_needs_xy") from None
        width, height = self.geometry()
        if not (0 <= x < width and 0 <= y < height):
            raise X11Error("point_outside_screen")
        return str(x), str(y)

    def active_app(self) -> str | None:
        """Foreground class from ``xprop WM_CLASS``, else the trusted PID."""
        try:
            active = self._run("xdotool", "getactivewindow", timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        wid = (active.stdout or "").strip()
        if active.returncode != 0 or not wid.isdigit():
            return None
        try:
            prop = self._run("xprop", "-id", wid, "WM_CLASS", timeout=5)
            from desktop_linux_browser import parse_wm_class

            parsed = parse_wm_class(prop.stdout or "")
            if parsed:
                label = (parsed[1] or parsed[0]).strip()
                if label:
                    return label[:64]
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            pid = self._run("xdotool", "getwindowpid", wid, timeout=5)
            text = (pid.stdout or "").strip()
            if pid.returncode == 0 and text.isdigit() and int(text) > 0:
                from desktop_linux_browser import linux_process_app

                app = linux_process_app(int(text))
                if app and app != "Unknown":
                    return app[:64]
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return None

    def active_window(self) -> dict:
        """Read-only focus identity on this display. Missing fields stay absent.

        The title is capped and never logged: it can be private UI text.
        """
        try:
            active = self._run("xdotool", "getactivewindow", timeout=5)
        except (OSError, subprocess.SubprocessError):
            return {}
        wid = (active.stdout or "").strip()
        if active.returncode != 0 or not wid.isdigit():
            return {}
        out: dict[str, Any] = {"window_id": int(wid)}
        try:
            pid = self._run("xdotool", "getwindowpid", wid, timeout=5)
            text = (pid.stdout or "").strip()
            if pid.returncode == 0 and text.isdigit() and int(text) > 0:
                out["pid"] = int(text)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            named = self._run("xdotool", "getwindowname", wid, timeout=5)
            title = (named.stdout or "").strip()
            if named.returncode == 0 and title:
                out["window_title"] = title[:120]
        except (OSError, subprocess.SubprocessError):
            pass
        return out

    def has_browser_window(self) -> bool:
        """True once a browser window is mapped.

        Matching any window would always succeed: the window manager owns
        several invisible-to-the-eye helper windows of its own.
        """
        try:
            found = self._run("xdotool", "search", "--onlyvisible", "--class", BROWSER_CLASSES, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        from desktop_linux_browser import xprop_browser_target

        for wid in (found.stdout or "").split():
            if not wid.isdigit():
                continue
            try:
                prop = self._run(
                    "xprop", "-id", wid, "WM_CLASS", "WM_STATE", "_NET_WM_WINDOW_TYPE", timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if xprop_browser_target(prop.stdout or ""):
                return True
        return False

    # ---- actions ------------------------------------------------------------

    def observe(self) -> dict:
        from desktop_cua import _prepare_cua_screenshot_path, _record_existing_cua_screenshot

        if not shutil.which("import"):
            raise X11Error("import_missing")
        prepared = _prepare_cua_screenshot_path(self.settings)
        if not prepared.get("ok"):
            raise X11Error("screenshot_path_error")
        path = Path(prepared["path"])
        captured = self._run("import", "-silent", "-window", "root", f"png:{path}", timeout=20, text=False)
        if captured.returncode != 0 or not path.exists():
            raise X11Error("capture_failed")
        recorded = _record_existing_cua_screenshot(
            self.settings, path, prepared["screenshot_id"], node_id=x11_node_id(agents.takeover_scope(self.agent_id)),
        )
        if not recorded.get("ok"):
            raise X11Error("screenshot_record_error")
        result = {
            "screenshot_id": recorded["screenshot_id"], "sha256": recorded["sha256"],
            "width": recorded["width"], "height": recorded["height"],
        }
        result.update(self.active_window())
        return result

    def click(self, action: dict) -> dict:
        x, y = self._point(action)
        button = {"left": "1", "middle": "2", "right": "3"}.get(str(action.get("button") or "left").lower())
        if button is None:
            raise X11Error("bad_button")
        self._xdotool("mousemove", x, y, "click", button)
        return {"click_method": "xtest"}

    def _require_keyboard_target(self, action: dict) -> None:
        """If the loop bound a window, type only while that window is in front.

        Agent desktops have no separate browser-controller fallback; this
        uses the same DISPLAY as the rest of the step.
        """
        pid = action.get("pid")
        wid = action.get("window_id")
        if pid is None and wid is None:
            return
        active = self._run("xdotool", "getactivewindow", timeout=5)
        current = (active.stdout or "").strip()
        if active.returncode != 0 or not current.isdigit():
            raise X11Error("keyboard_target_not_foreground")
        if wid is not None and int(wid) != int(current):
            raise X11Error("keyboard_target_not_foreground")
        if pid is not None:
            got = self._run("xdotool", "getwindowpid", current, timeout=5)
            if (got.stdout or "").strip() != str(int(pid)):
                raise X11Error("keyboard_target_not_foreground")

    def type_text(self, action: dict) -> dict:
        self._require_keyboard_target(action)
        text = action.get("text")
        if not isinstance(text, str) or not text:
            raise X11Error("type_needs_text")
        for start in range(0, len(text), TYPE_CHUNK):
            # `type` takes everything after `--` literally, one call per chunk.
            self._xdotool("type", "--clearmodifiers", "--delay", "8", "--", text[start:start + TYPE_CHUNK])
        return {"text_len": len(text)}

    def hotkey(self, action: dict) -> dict:
        self._require_keyboard_target(action)
        keys = action.get("keys")
        self._xdotool("key", "--clearmodifiers", hotkey_argument(keys))
        return {"keys_len": len(keys)}

    def scroll(self, action: dict) -> dict:
        try:
            dx, dy = float(action.get("dx") or 0), float(action.get("dy") or 0)
        except (TypeError, ValueError):
            raise X11Error("bad_scroll") from None
        if not dx and not dy:
            raise X11Error("bad_scroll")
        vertical = abs(dy) >= abs(dx)
        delta = dy if vertical else dx
        button = ("5" if delta > 0 else "4") if vertical else ("7" if delta > 0 else "6")
        steps = str(max(1, min(int(round(abs(delta) / 120)) or 1, 15)))
        args: list[str] = []
        if action.get("x") is not None and action.get("y") is not None:
            args += ["mousemove", *self._point(action)]
        self._xdotool(*args, "click", "--repeat", steps, button)
        return {}

    def execute(self, action: dict) -> dict:
        kind = action.get("action")
        try:
            if kind == "observe":
                result = self.observe()
            elif kind == "click":
                result = self.click(action)
            elif kind == "type":
                result = self.type_text(action)
            elif kind == "hotkey":
                result = self.hotkey(action)
            elif kind == "scroll":
                result = self.scroll(action)
            elif kind == "wait":
                time.sleep(max(0.0, min(float(action.get("seconds") or 1), MAX_WAIT_SECONDS)))
                result = {}
            else:
                raise X11Error("unsupported_action")
        except X11Error as exc:
            return {"result_ok": False, "error": str(exc), "action_type": str(kind)}
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            return {"result_ok": False, "error": "x11_failed", "action_type": str(kind)}
        result.update({"result_ok": True, "action_type": str(kind)})
        app = self.active_app()
        if app:
            result["active_app"] = app
        for key, value in self.active_window().items():
            result.setdefault(key, value)
        return result


class X11ComputerBackend:
    """Run a task's steps on the agent's own display, in this process."""

    def __init__(self, settings: Settings, *, agent_id: str, display: int, scope: str) -> None:
        self.settings = settings
        self.agent_id = agent_id
        self.node_id = x11_node_id(scope)
        self.desktop = X11Desktop(settings, agent_id=agent_id, display=display)
        self._prepared = False

    def _takeover_active(self, settings: Settings) -> bool:
        from human_takeover import HumanTakeoverStore

        return HumanTakeoverStore(settings).current(agents.takeover_scope(self.agent_id)) is not None

    async def _prepare(self) -> None:
        """A bare desktop has nothing to look at: have the browser up first.

        A human lease is checked before any launch and again before waiting,
        so preparation never starts a browser the operator just took over.
        """
        if self._prepared:
            return
        if self._takeover_active(self.settings):
            from desktop_computer_loop import ComputerBackendError
            raise ComputerBackendError("human_takeover_active")
        if await asyncio.to_thread(self.desktop.has_browser_window):
            self._prepared = True
            return
        if self._takeover_active(self.settings):
            from desktop_computer_loop import ComputerBackendError
            raise ComputerBackendError("human_takeover_active")
        from agent_desktops import request_browser

        request_browser(self.settings, self.agent_id)
        self._prepared = True
        deadline = time.monotonic() + BROWSER_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._takeover_active(self.settings):
                from desktop_computer_loop import ComputerBackendError
                raise ComputerBackendError("human_takeover_active")
            await asyncio.sleep(1.0)
            if await asyncio.to_thread(self.desktop.has_browser_window):
                # Let the first page paint before the first screenshot.
                await asyncio.sleep(3.0)
                return
        logger.warning("agent %s: no window appeared on its desktop", self.agent_id)

    async def execute_step(self, settings: Settings, task_id: str, step_id: str, action: dict) -> dict:
        from desktop_computer_loop import ComputerBackendError

        if self._takeover_active(settings):
            raise ComputerBackendError("human_takeover_active")
        await self._prepare()
        if self._takeover_active(settings):
            raise ComputerBackendError("human_takeover_active")
        claimed = claim_computer_step(settings, step_id, self.node_id)
        if not claimed.get("ok"):
            error = str(claimed.get("error") or "claim_failed")
            raise ComputerBackendError("human_takeover_active" if error == "human_takeover_active" else f"step_{error}")
        if self._takeover_active(settings):
            raise ComputerBackendError("human_takeover_active")
        result = await asyncio.to_thread(self.desktop.execute, claimed["step"].get("action") or action)
        completed = complete_computer_step(settings, step_id, self.node_id, result)
        if not completed.get("ok"):
            raise ComputerBackendError(str(completed.get("error") or "complete_failed"))
        return result
