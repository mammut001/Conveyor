"""live_screen.py — embedded live view and one-click takeover of the host desktop.

The Web Console shows the host's X11 screen as a stream of JPEG frames and,
when the operator takes control, forwards mouse and keyboard input to it.
Everything goes through the console's own authenticated HTTP API: no VNC
port, no second password, no extra tab.

Safety contract (shared with docs/human_takeover.md):

- Taking control opens the same exclusive lease as Secure Human Takeover, so
  the computer-use loop and Agent screenshots pause until it is released.
- Input is accepted only while this process holds that lease.
- Frames live in memory for the authenticated operator only; they are never
  written to disk, logged, or handed to the Agent.
- Typed text and key names are never logged.
- The lease is short and renewed by an open viewer, so a closed tab or a
  crashed console releases the desktop on its own.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from human_takeover import DEFAULT_SCOPE, HumanTakeoverStore

logger = logging.getLogger("conveyor.live_screen")

LEASE_OWNER = "live-screen"
LEASE_TTL_SECONDS = 120
LEASE_RENEW_EVERY_SECONDS = 20
VIEWER_IDLE_SECONDS = 10
CONTROL_IDLE_SECONDS = 45
WAKE_EVERY_SECONDS = 50
# Right after the operator acts, the screen is what they are waiting for:
# capture at this pace for a moment instead of the idle viewing rate.
ACTIVE_INTERVAL_SECONDS = 0.1
ACTIVE_WINDOW_SECONDS = 1.5
# A viewer's first frame must be a settled one: right after the screensaver
# is dismissed the desktop is still black for about half a second. Publish
# only once two captures in a row agree, or after this long regardless.
SETTLE_INTERVAL_SECONDS = 0.15
SETTLE_MAX_SECONDS = 1.5
NO_POINTER = (-1, -1)
# time.monotonic() may start near zero, so "never happened" cannot be 0.0.
NEVER = float("-inf")
# Screensaver daemons that may be covering the desktop: (binary, query
# arguments, dismiss arguments). Missing tools are skipped.
SCREENSAVERS = (
    ("xfce4-screensaver-command", ("--query",), ("--deactivate",)),
    ("gnome-screensaver-command", ("--query",), ("--deactivate",)),
    ("mate-screensaver-command", ("--query",), ("--deactivate",)),
)
# How long a dismissed screensaver takes to actually leave the screen. The
# black picture is stable for most of that time, so "two equal captures"
# cannot detect it; measured at about half a second on XFCE.
DISMISS_SETTLE_SECONDS = 0.8
IN_FLIGHT_WAIT_SECONDS = 20
MAX_EVENTS_PER_REQUEST = 64
MAX_TEXT_CHARS = 500
SESSION_PROCESS_NAMES = (
    "xfce4-session", "gnome-session", "gnome-session-binary", "plasmashell",
    "mate-session", "cinnamon-session", "lxsession", "openbox", "i3",
)
SESSION_ENV_KEYS = ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")

# Browser KeyboardEvent.key -> X keysym, for keys that are not one character.
KEYSYMS = {
    "Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab", "Escape": "Escape",
    "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End",
    "PageUp": "Prior", "PageDown": "Next", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ArrowUp": "Up", "ArrowDown": "Down", " ": "space", "CapsLock": "Caps_Lock",
    "ContextMenu": "Menu", "PrintScreen": "Print", "Pause": "Pause",
    **{f"F{n}": f"F{n}" for n in range(1, 13)},
}
MODIFIERS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "meta": "super"}
SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG's frame header, or (0, 0).

    The frame itself is the only size that cannot go stale: the desktop can
    be resized (an RDP reconnect, xrandr) while a viewer is watching.
    """
    index, end = 2, len(data)
    while index + 9 < end:
        if data[index] != 0xFF:
            return (0, 0)
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte
            index += 1
            continue
        # SOF0..SOF15 carry the dimensions; C4, C8 and CC are other tables.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height)
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return (0, 0)


class LiveScreenError(RuntimeError):
    """Operator-facing failure; the message is safe to return over the API."""


def _keysym(key: str) -> str | None:
    if key in KEYSYMS:
        return KEYSYMS[key]
    if len(key) != 1:
        return None
    if key.isascii() and key.isalnum():
        return key
    # Xlib accepts any Unicode code point as a "Uxxxx" keysym name.
    return f"U{ord(key):04X}"


class LiveScreen:
    def __init__(
        self, settings: Any, *, display: str = "", xauthority: str = "", scope: str = DEFAULT_SCOPE,
    ) -> None:
        """One screen. With `display` it is a fixed X display (an agent's own
        desktop) whose takeover lease lives in `scope`; without, the host's
        desktop session is discovered and the default scope is used."""
        self.settings = settings
        self._fixed_display = display
        self._fixed_xauthority = xauthority
        self._scope = scope
        self._lock = threading.Lock()
        self._frame_ready = threading.Condition(self._lock)
        self._frame: bytes = b""
        self._frame_digest = ""
        self._seq = 0
        self._size: tuple[int, int] = (0, 0)
        self._capture_error = ""
        self._last_viewer_at = NEVER
        self._worker: threading.Thread | None = None
        self._env: dict[str, str] | None = None
        self._env_checked_at = NEVER
        self._woke_at = NEVER
        self._last_input_at = NEVER
        self._pointer_at: tuple[int, int] = NO_POINTER
        self._settle_started = NEVER
        self._settle_digest = ""
        self._kick = threading.Event()
        self._control_id: str | None = None
        self._control_seen_at = NEVER
        self._control_renewed_at = NEVER
        self._store: HumanTakeoverStore | None = None

    # ---- configuration ----------------------------------------------------

    @property
    def enabled(self) -> bool:
        # `is True`: a mocked or partial settings object must not switch this on.
        return getattr(self.settings, "live_screen_enabled", False) is True

    @property
    def control_enabled(self) -> bool:
        return self.enabled and getattr(self.settings, "live_screen_control_enabled", True) is True

    @property
    def _interval(self) -> float:
        fps = float(getattr(self.settings, "live_screen_fps", 4.0) or 4.0)
        return 1.0 / max(0.5, min(fps, 10.0))

    @property
    def _quality(self) -> str:
        return str(max(20, min(int(getattr(self.settings, "live_screen_quality", 60) or 60), 90)))

    def _takeover_store(self) -> HumanTakeoverStore:
        if self._store is None:
            self._store = HumanTakeoverStore(self.settings)
        return self._store

    # ---- display discovery -------------------------------------------------

    def _display_env(self) -> dict[str, str] | None:
        """Environment of the graphical session to capture, or None."""
        now = time.monotonic()
        if self._env is not None and now - self._env_checked_at < 30:
            return self._env
        self._env_checked_at = now
        found: dict[str, str] = {}
        configured = str(getattr(self.settings, "live_screen_display", "") or "").strip()
        if self._fixed_display:
            found["DISPLAY"] = self._fixed_display
            if self._fixed_xauthority:
                found["XAUTHORITY"] = self._fixed_xauthority
        elif configured:
            found["DISPLAY"] = configured
        elif os.getenv("DISPLAY"):
            found = {k: os.environ[k] for k in SESSION_ENV_KEYS if os.getenv(k)}
        else:
            found = self._session_env_from_proc()
        if not found.get("DISPLAY"):
            self._env = None
            return None
        found.setdefault("XAUTHORITY", os.getenv("XAUTHORITY") or str(Path.home() / ".Xauthority"))
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "LC_ALL")}
        env.update(found)
        self._env = env
        return env

    @staticmethod
    def _session_env_from_proc() -> dict[str, str]:
        """Read DISPLAY etc. from this user's desktop session process (Linux)."""
        proc = Path("/proc")
        if not proc.is_dir():
            return {}
        uid = os.getuid()
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                if (entry / "comm").read_text().strip() not in SESSION_PROCESS_NAMES:
                    continue
                raw = (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            env = {}
            for item in raw:
                key, sep, value = item.partition(b"=")
                if sep and key.decode("ascii", "replace") in SESSION_ENV_KEYS:
                    env[key.decode()] = value.decode("utf-8", "replace")
            if env.get("DISPLAY"):
                return env
        return {}

    def _unavailable_reason(self) -> str:
        if not self.enabled:
            return "Live screen is disabled (set CONVEYOR_LIVE_SCREEN_ENABLED=true)"
        if not shutil.which("import"):
            return "ImageMagick `import` is not installed on the host"
        if self._display_env() is None:
            return "No graphical session found on the host"
        return ""

    # ---- capture ------------------------------------------------------------

    def _capture_once(self) -> bytes:
        env = self._display_env()
        if env is None:
            raise LiveScreenError("No graphical session found on the host")
        result = subprocess.run(
            ["import", "-silent", "-window", "root", "-quality", self._quality, "jpg:-"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False,
        )
        if result.returncode != 0 or not result.stdout:
            # A stale DISPLAY (session restarted) must be rediscovered.
            self._env = None
            raise LiveScreenError("Screen capture failed")
        return result.stdout

    def _wake_display(self) -> bool:
        """Keep a screensaver from covering the desktop while someone watches.

        An idle headless desktop blanks itself, and a viewer would only ever
        see black. A locked session still shows its unlock prompt. Returns
        True when a screensaver was showing and had to be dismissed.
        """
        env = self._display_env()
        if env is None or self._fixed_display:
            # A bare agent desktop runs no screensaver, and its environment has
            # no session bus to ask one.
            return False

        def run(*command: str) -> str:
            try:
                return subprocess.run(
                    list(command), env=env, timeout=5, check=False, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                ).stdout or ""
            except (OSError, subprocess.SubprocessError):
                return ""

        if shutil.which("xset"):
            run("xset", "s", "reset")  # restart the X idle timer
        dismissed = False
        for binary, query, dismiss in SCREENSAVERS:
            if not shutil.which(binary):
                continue
            state = run(binary, *query).lower()
            if "inactive" in state:
                continue
            # An unreadable answer still gets a dismiss (harmless), but only a
            # screensaver that said it was showing is worth waiting for.
            run(binary, *dismiss)
            dismissed = dismissed or "active" in state
        return dismissed

    def _pointer(self) -> tuple[int, int]:
        """Where the host pointer is, so a viewer can see what the Agent points at.

        Screen captures do not include the cursor image.
        """
        env = self._display_env()
        if env is None or not shutil.which("xdotool"):
            return NO_POINTER
        try:
            out = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"], env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
            ).stdout
            values = dict(line.split("=", 1) for line in out.split() if "=" in line)
            return (int(values["X"]), int(values["Y"]))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return NO_POINTER

    def _geometry(self) -> tuple[int, int]:
        env = self._display_env()
        if env is None or not shutil.which("xdotool"):
            return (0, 0)
        try:
            out = subprocess.run(
                ["xdotool", "getdisplaygeometry"], env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
            ).stdout.split()
            return (int(out[0]), int(out[1]))
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return (0, 0)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, name="live-screen", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        """Capture while someone is watching; keep the control lease honest."""
        while True:
            now = time.monotonic()
            with self._lock:
                watching = now - self._last_viewer_at < VIEWER_IDLE_SECONDS
                controlling = self._control_id is not None
                if not watching and not controlling:
                    self._worker = None
                    return
            if controlling:
                self._tend_control(now)
            if watching:
                started = time.monotonic()
                if started - self._woke_at >= WAKE_EVERY_SECONDS:
                    self._woke_at = started
                    if self._wake_display():
                        time.sleep(DISMISS_SETTLE_SECONDS)
                try:
                    data = self._capture_once()
                    size = _jpeg_size(data)
                    if size == (0, 0):
                        size = self._geometry()
                    pointer = self._pointer()
                    # The pointer is part of what a viewer sees, so a move
                    # with an unchanged picture still counts as a new frame.
                    digest = hashlib.blake2b(data + repr(pointer).encode(), digest_size=12).hexdigest()
                    settling = False
                    with self._frame_ready:
                        self._capture_error = ""
                        if not self._frame:
                            if self._settle_started == NEVER:
                                self._settle_started = started
                            settling = (
                                digest != self._settle_digest
                                and started - self._settle_started < SETTLE_MAX_SECONDS
                            )
                            self._settle_digest = digest
                        if not settling:
                            self._size = size
                            self._pointer_at = pointer
                            if digest != self._frame_digest:
                                self._frame, self._frame_digest = data, digest
                                self._seq += 1
                                self._frame_ready.notify_all()
                    if settling:
                        time.sleep(SETTLE_INTERVAL_SECONDS)
                        continue
                except (LiveScreenError, OSError, subprocess.SubprocessError) as exc:
                    with self._frame_ready:
                        self._capture_error = str(exc) if isinstance(exc, LiveScreenError) else "Screen capture failed"
                        self._size = (0, 0)
                        self._frame_ready.notify_all()
                    time.sleep(1.0)
                active = time.monotonic() - self._last_input_at < ACTIVE_WINDOW_SECONDS
                interval = min(self._interval, ACTIVE_INTERVAL_SECONDS) if active else self._interval
                # Input cuts the wait short so its effect shows up at once.
                self._kick.wait(max(0.02, interval - (time.monotonic() - started)))
                self._kick.clear()
            else:
                time.sleep(1.0)

    def frame(
        self, since: int = 0, wait: float = 1.5,
    ) -> tuple[int, bytes, tuple[int, int], tuple[int, int]] | None:
        """Return the newest frame if it is newer than `since`, waiting briefly."""
        reason = self._unavailable_reason()
        if reason:
            raise LiveScreenError(reason)
        now = time.monotonic()
        with self._lock:
            if now - self._last_viewer_at > VIEWER_IDLE_SECONDS:
                # Nobody was watching, so capture had stopped: the frame in
                # memory may be hours old. Wait for a fresh one instead.
                self._frame, self._frame_digest = b"", ""
                self._settle_started, self._settle_digest = NEVER, ""
                # Check for a screensaver again before the first new frame.
                self._woke_at = NEVER
            self._last_viewer_at = now
            if self._control_id is not None:
                self._control_seen_at = now
        self._ensure_worker()
        deadline = now + max(0.0, min(wait, 5.0))
        with self._frame_ready:
            while self._seq <= since or not self._frame:
                if self._capture_error:
                    raise LiveScreenError(self._capture_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frame_ready.wait(remaining)
            return self._seq, self._frame, self._size, self._pointer_at

    # ---- control lease -----------------------------------------------------

    def _current_control(self) -> dict[str, Any] | None:
        """The open lease if this process owns it; forget it once it is gone."""
        with self._lock:
            control_id = self._control_id
        if control_id is None:
            return None
        current = self._takeover_store().current(self._scope)
        if current and current.get("id") == control_id:
            return current
        with self._lock:
            if self._control_id == control_id:
                self._control_id = None
        return None

    def _tend_control(self, now: float) -> None:
        with self._lock:
            control_id = self._control_id
            idle = now - self._control_seen_at
            renew_due = now - self._control_renewed_at >= LEASE_RENEW_EVERY_SECONDS
        if control_id is None:
            return
        if idle > CONTROL_IDLE_SECONDS:
            logger.info("live screen control released: viewer went away")
            self.release_control()
        elif renew_due:
            if self._takeover_store().extend(control_id, LEASE_TTL_SECONDS) is None:
                with self._lock:
                    if self._control_id == control_id:
                        self._control_id = None
            else:
                with self._lock:
                    self._control_renewed_at = now

    def take_control(self) -> dict[str, Any]:
        if not self.control_enabled:
            raise LiveScreenError("Live screen control is disabled")
        reason = self._unavailable_reason()
        if reason:
            raise LiveScreenError(reason)
        if not shutil.which("xdotool"):
            raise LiveScreenError("xdotool is not installed on the host")
        if self._current_control() is not None:
            return self.status()

        store = self._takeover_store()
        existing = store.current(self._scope)
        if existing is not None:
            if existing.get("requested_by") != LEASE_OWNER:
                raise LiveScreenError("Another human takeover is already open")
            # Left behind by a previous console process: adopt it.
            lease = existing
        else:
            lease = store.start(
                reason="operator_requested", requested_by=LEASE_OWNER, ttl_seconds=LEASE_TTL_SECONDS,
                scope=self._scope,
            )
        lease_id = str(lease["id"])

        # Same ordering as WebTakeover.start: the Agent must be idle before a
        # human click can land, or both would drive the pointer at once.
        from desktop_computer_requests import cancel_pending_computer_steps, has_claimed_computer_steps
        from desktop_observe_requests import cancel_pending_observe_requests, has_claimed_observe_requests

        # Queued computer-use work targets the shared host desktop. Taking over
        # an agent's own desktop must not cancel it.
        if self._scope == DEFAULT_SCOPE:
            cancel_pending_computer_steps(self.settings)
            cancel_pending_observe_requests(self.settings)
            deadline = time.monotonic() + IN_FLIGHT_WAIT_SECONDS
            while has_claimed_computer_steps(self.settings) or has_claimed_observe_requests(self.settings):
                if time.monotonic() >= deadline:
                    store.cancel(lease_id)
                    raise LiveScreenError("The Agent is still finishing an action; try again in a moment")
                time.sleep(0.1)
        store.activate(lease_id)

        now = time.monotonic()
        with self._lock:
            self._control_id = lease_id
            self._control_seen_at = now
            self._control_renewed_at = now
            self._last_viewer_at = now
        self._ensure_worker()
        logger.info("live screen control taken (lease %s)", lease_id[:8])
        return self.status()

    def release_control(self) -> dict[str, Any]:
        with self._lock:
            control_id, self._control_id = self._control_id, None
        if control_id is not None:
            self._release_held_inputs()
            self._takeover_store().complete(control_id)
            logger.info("live screen control released (lease %s)", control_id[:8])
        return self.status()

    def _release_held_inputs(self) -> None:
        """Never hand the desktop back with a button or modifier stuck down."""
        env = self._display_env()
        if env is None or not shutil.which("xdotool"):
            return
        try:
            subprocess.run(
                ["xdotool", "mouseup", "1", "mouseup", "2", "mouseup", "3",
                 "keyup", "ctrl", "keyup", "alt", "keyup", "shift", "keyup", "super"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # ---- input ---------------------------------------------------------------

    def send_input(self, events: Any) -> dict[str, Any]:
        if not isinstance(events, list) or not events:
            raise ValueError("events must be a non-empty list")
        if len(events) > MAX_EVENTS_PER_REQUEST:
            raise ValueError(f"at most {MAX_EVENTS_PER_REQUEST} events per request")
        if self._current_control() is None:
            raise LiveScreenError("Take control before sending input")
        env = self._display_env()
        if env is None:
            raise LiveScreenError("No graphical session found on the host")
        # Ask the display, not the last frame: a click must be checked
        # against the screen as it is now.
        width, height = self._geometry()

        commands = self._translate(events, width, height)
        now = time.monotonic()
        with self._lock:
            self._control_seen_at = now
            self._last_viewer_at = now
        for args in commands:
            try:
                subprocess.run(
                    ["xdotool", *args], env=env, timeout=10, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                raise LiveScreenError("Input injection failed") from None
        self._last_input_at = time.monotonic()
        self._kick.set()
        return {"ok": True, "applied": len(events)}

    @staticmethod
    def _point(event: dict[str, Any], width: int, height: int) -> list[str]:
        x, y = int(event["x"]), int(event["y"])
        if x < 0 or y < 0 or (width and x >= width) or (height and y >= height):
            raise ValueError("pointer position is outside the screen")
        return ["mousemove", str(x), str(y)]

    @classmethod
    def _translate(cls, events: list[Any], width: int, height: int) -> list[list[str]]:
        """Turn browser events into xdotool invocations (validated, no shell)."""
        commands: list[list[str]] = []
        chain: list[str] = []

        def flush() -> None:
            if chain:
                commands.append(list(chain))
                chain.clear()

        for event in events:
            if not isinstance(event, dict):
                raise ValueError("each event must be an object")
            kind = event.get("t")
            try:
                if kind == "move":
                    chain.extend(cls._point(event, width, height))
                elif kind in ("down", "up", "click"):
                    button = int(event.get("b", 1))
                    if button not in (1, 2, 3):
                        raise ValueError("unsupported mouse button")
                    chain.extend(cls._point(event, width, height))
                    if kind == "click":
                        repeat = max(1, min(int(event.get("n", 1)), 3))
                        chain.extend(["click", "--repeat", str(repeat), str(button)])
                    else:
                        chain.extend(["mousedown" if kind == "down" else "mouseup", str(button)])
                elif kind == "scroll":
                    direction = str(event.get("dir", ""))
                    if direction not in SCROLL_BUTTONS:
                        raise ValueError("unsupported scroll direction")
                    chain.extend(cls._point(event, width, height))
                    steps = max(1, min(int(event.get("n", 1)), 10))
                    chain.extend(["click", "--repeat", str(steps), SCROLL_BUTTONS[direction]])
                elif kind == "key":
                    keysym = _keysym(str(event.get("key", "")))
                    if keysym is None:
                        continue
                    mods = [MODIFIERS[m] for m in event.get("mods") or [] if m in MODIFIERS]
                    chain.extend(["key", "--clearmodifiers", "+".join([*mods, keysym])])
                elif kind == "text":
                    text = str(event.get("text", ""))
                    if not text or len(text) > MAX_TEXT_CHARS:
                        raise ValueError(f"text must be 1-{MAX_TEXT_CHARS} characters")
                    flush()
                    # `type` swallows every following argument, so it runs alone.
                    commands.append(["type", "--clearmodifiers", "--delay", "8", "--", text])
                else:
                    raise ValueError("unsupported event type")
            except (KeyError, TypeError):
                raise ValueError("malformed input event") from None
        flush()
        return commands

    # ---- status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        reason = self._unavailable_reason()
        lease = self._current_control() if self.enabled else None
        other = None
        if self.enabled and lease is None:
            current = self._takeover_store().current(self._scope)
            if current is not None:
                other = str(current.get("requested_by") or "human-takeover")
        with self._lock:
            width, height = self._size
        return {
            "enabled": self.enabled,
            "available": not reason,
            "reason": reason,
            "control_enabled": self.control_enabled and bool(shutil.which("xdotool")),
            "controlling": lease is not None,
            "agent_paused": lease is not None or other is not None,
            "blocked_by": other,
            # True for an agent's own desktop, False for the shared host one.
            "dedicated": bool(self._fixed_display),
            "width": width,
            "height": height,
        }
