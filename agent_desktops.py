"""agent_desktops.py — keeps one always-on virtual desktop per agent.

Each agent with a display number gets its own X server (Xvfb), a window
manager, and — once someone asks for it — a browser with a profile that
belongs to that agent alone. This supervisor is the only thing that starts or
stops them; the Web Console just records what it wants in the agents table
and in small request files, so it needs no privileges and no systemd access.

Desktops outlive the supervisor: children run in their own sessions, the unit
uses ``KillMode=process``, and a restarted supervisor adopts what is already
running. A deploy therefore does not close anybody's windows.

    python agent_desktops.py            run the supervisor
    python agent_desktops.py --once     reconcile once and exit (diagnostics)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import agents

logger = logging.getLogger("conveyor.agent_desktops")

POLL_SECONDS = 2.0
PIDS_FILE = "pids.json"
BROWSER_REQUEST_FILE = "want_browser"
BACKGROUND = "#1f2937"
SNAP_FIREFOX = Path("/snap/bin/firefox")


def parse_size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in str(value).lower().split("x", 1))
    except ValueError:
        return (1440, 900)
    if not (640 <= width <= 3840 and 480 <= height <= 2160):
        return (1440, 900)
    return (width, height)


def request_browser(settings: Any, agent_id: str) -> None:
    """Ask the supervisor to open the agent's browser (idempotent, unprivileged)."""
    directory = agents.desktop_dir(settings, agent_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BROWSER_REQUEST_FILE).write_text(str(time.time()), encoding="utf-8")


def _pid_alive(pid: int, needle: str = "") -> bool:
    """True if `pid` exists and (when given) its command line contains `needle`.

    The needle guards against a recycled pid after a reboot.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    if not needle:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return True  # no /proc (not Linux): existence is the best we have
    return needle in cmdline


def _spawn(command: list[str], env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,  # survive a supervisor restart
        )
    return process.pid


def _terminate(pid: int) -> None:
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(20):
            if not _pid_alive(pid):
                return
            time.sleep(0.1)


class Supervisor:
    def __init__(
        self,
        settings: Any,
        *,
        spawn: Callable[[list[str], dict[str, str], Path], int] = _spawn,
        alive: Callable[[int, str], bool] = _pid_alive,
        terminate: Callable[[int], None] = _terminate,
    ) -> None:
        self.settings = settings
        self._spawn, self._alive, self._terminate = spawn, alive, terminate
        self.size = parse_size(getattr(settings, "agent_desktop_size", "1440x900"))

    # ---- per-agent state ---------------------------------------------------

    def _dir(self, agent_id: str) -> Path:
        directory = agents.desktop_dir(self.settings, agent_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory

    def _load(self, agent_id: str) -> dict[str, Any]:
        try:
            value = json.loads((self._dir(agent_id) / PIDS_FILE).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, agent_id: str, state: dict[str, Any]) -> None:
        path = self._dir(agent_id) / PIDS_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)

    def _env(self, agent_id: str, display: int) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL")}
        env.setdefault("HOME", str(Path.home()))
        env["DISPLAY"] = f":{display}"
        env["XAUTHORITY"] = str(agents.xauthority_path(self.settings, agent_id))
        # A user-session bus lets desktop apps (and snap packages) start.
        runtime = Path(f"/run/user/{os.getuid()}")
        if (runtime / "bus").exists():
            env["XDG_RUNTIME_DIR"] = str(runtime)
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
        return env

    def _stop_tracked(self, state: dict[str, Any]) -> None:
        """Stop the processes a pids file names, but only if they are still ours.

        After a reboot the recorded pids may belong to something else entirely.
        """
        display = state.get("display")
        for key, needle in (("browser", "firefox"), ("wm", "xfwm4"), ("xvfb", f"Xvfb :{display} ")):
            pid = int(state.get(key) or 0)
            if self._alive(pid, needle):
                self._terminate(pid)

    # ---- desktop -----------------------------------------------------------

    def _write_xauthority(self, agent_id: str, display: int) -> bool:
        """A fresh cookie: only holders of this file can see or drive the screen."""
        path = agents.xauthority_path(self.settings, agent_id)
        path.unlink(missing_ok=True)
        path.touch(mode=0o600)
        if not shutil.which("xauth"):
            logger.error("xauth is not installed; refusing to start an unauthenticated display")
            return False
        result = subprocess.run(
            ["xauth", "-f", str(path), "add", f":{display}", "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        return result.returncode == 0

    def _display_ready(self, env: dict[str, str]) -> bool:
        if not shutil.which("xdpyinfo"):
            return True
        return subprocess.run(
            ["xdpyinfo"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
        ).returncode == 0

    def ensure_desktop(self, agent_id: str, display: int) -> bool:
        """Start, or adopt, the X server and window manager. True when up."""
        state = self._load(agent_id)
        env = self._env(agent_id, display)
        directory = self._dir(agent_id)
        needle = f"Xvfb :{display} "

        if state.get("display") != display or not self._alive(int(state.get("xvfb") or 0), needle):
            # Nothing of ours to adopt: whatever else we tracked is stale too.
            self._stop_tracked(state)
            Path(f"/tmp/.X{display}-lock").unlink(missing_ok=True)
            if not self._write_xauthority(agent_id, display):
                return False
            width, height = self.size
            xvfb = self._spawn(
                ["Xvfb", f":{display}", "-screen", "0", f"{width}x{height}x24",
                 "-nolisten", "tcp", "-auth", env["XAUTHORITY"], "-noreset"],
                env, directory / "xvfb.log",
            )
            state = {"display": display, "xvfb": xvfb, "started_at": time.time()}
            self._save(agent_id, state)
            for _ in range(50):
                if self._display_ready(env):
                    break
                time.sleep(0.1)
            else:
                logger.error("agent %s: display :%d did not come up", agent_id, display)
                return False
            if shutil.which("xsetroot"):
                subprocess.run(["xsetroot", "-solid", BACKGROUND], env=env, timeout=5, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("agent %s: desktop up on :%d (%dx%d)", agent_id, display, width, height)

        if not self._alive(int(state.get("wm") or 0), "xfwm4") and shutil.which("xfwm4"):
            state["wm"] = self._spawn(["xfwm4", "--compositor=off"], env, directory / "wm.log")
            self._save(agent_id, state)
        return True

    # ---- browser -----------------------------------------------------------

    def _browser_command(self, agent_id: str) -> list[str] | None:
        width, height = self.size
        if SNAP_FIREFOX.exists():
            # A snap can only write inside its own home, so the profile lives there.
            profile = Path.home() / "snap" / "firefox" / "common" / "conveyor-agents" / agent_id
            binary = str(SNAP_FIREFOX)
        else:
            binary = shutil.which("firefox") or shutil.which("firefox-esr") or ""
            profile = self._dir(agent_id) / "browser-profile"
        if not binary:
            return None
        profile.mkdir(parents=True, exist_ok=True)
        return [binary, "--no-remote", "--profile", str(profile), "--width", str(width), "--height", str(height)]

    def ensure_browser(self, agent_id: str, display: int) -> None:
        """Open the browser once per request; a browser the operator closed stays closed."""
        request = self._dir(agent_id) / BROWSER_REQUEST_FILE
        if not request.exists():
            return
        state = self._load(agent_id)
        if self._alive(int(state.get("browser") or 0), "firefox"):
            request.unlink(missing_ok=True)
            return
        command = self._browser_command(agent_id)
        request.unlink(missing_ok=True)
        if command is None:
            logger.warning("agent %s: no browser is installed on this host", agent_id)
            return
        state["browser"] = self._spawn(command, self._env(agent_id, display), self._dir(agent_id) / "browser.log")
        self._save(agent_id, state)
        logger.info("agent %s: browser started", agent_id)

    # ---- reconcile -----------------------------------------------------------

    def stop(self, agent_id: str) -> None:
        state = self._load(agent_id)
        self._stop_tracked(state)
        directory = agents.desktop_dir(self.settings, agent_id)
        for name in (PIDS_FILE, "Xauthority", BROWSER_REQUEST_FILE):
            (directory / name).unlink(missing_ok=True)
        if state:
            logger.info("agent %s: desktop stopped", agent_id)

    def reconcile(self) -> dict[str, int]:
        """Make running desktops match the agents table. Returns {agent_id: display}."""
        if not agents.desktops_enabled(self.settings):
            wanted: dict[str, int] = {}
        else:
            store = agents.AgentStore(self.settings)
            wanted = {}
            for agent in store.list():
                display = agent["display"] if agent["display"] is not None else store.ensure_display(agent["id"])
                if display is not None:
                    wanted[agent["id"]] = int(display)

        root = agents.desktop_dir(self.settings, "x").parent
        known = {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()
        for agent_id in sorted(known - set(wanted)):
            if (root / agent_id / PIDS_FILE).exists():
                self.stop(agent_id)
        for agent_id, display in wanted.items():
            try:
                if self.ensure_desktop(agent_id, display):
                    self.ensure_browser(agent_id, display)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.error("agent %s: %s", agent_id, exc)
        return wanted


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep one always-on virtual desktop per agent.")
    parser.add_argument("--once", action="store_true", help="reconcile once and exit")
    args = parser.parse_args()

    from config import load_runtime_settings
    from logging_setup import configure_logging

    configure_logging(service_name="agent_desktops")
    settings = load_runtime_settings()
    supervisor = Supervisor(settings)
    if not agents.desktops_enabled(settings):
        logger.info("Agent desktops are disabled (set CONVEYOR_AGENTS_ENABLED and CONVEYOR_AGENT_DESKTOPS_ENABLED)")
    if args.once:
        print(json.dumps(supervisor.reconcile()))
        return
    logger.info("Agent desktop supervisor started")
    while True:
        try:
            supervisor.reconcile()
        except Exception:
            logger.exception("reconcile failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
