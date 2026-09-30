#!/usr/bin/env python3
"""Systemd-owned remote-desktop lifecycle for Secure Human Takeover.

The Web Console never launches x11vnc/websockify.  It only creates a takeover
lease or writes a close request.  This sidecar observes that secret-free state,
invokes the fixed ``scripts/novnc_handoff.sh`` helper, and only finalizes a
lease after transport cleanup has been verified.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import load_settings
from human_takeover import HumanTakeoverStore
from web_takeover import (
    clear_close_request,
    read_close_request,
    read_sidecar_status,
    sidecar_status_path,
)

POLL_SECONDS = 0.5
_STOP = False


def _runtime_state_dir() -> Path:
    runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not runtime_base.is_dir() or not os.access(runtime_base, os.W_OK):
        runtime_base = Path(f"/tmp/conveyor-handoff-{os.getuid()}")
    return Path(os.environ.get("CONVEYOR_HANDOFF_STATE_DIR") or runtime_base / "conveyor-handoff")


def _pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError, OSError):
        return False


def transport_running() -> bool:
    state = _runtime_state_dir()
    # The marker is intentionally considered live until the helper has verified
    # the Tailscale Serve route is gone.
    if (state / "tailscale-serve.port").exists():
        return True
    return _pid_alive(state / "x11vnc.pid") or _pid_alive(state / "websockify.pid")


def _safe_url(raw: str) -> str | None:
    raw = str(raw or "").strip()
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return raw


def _extract_urls(output: str) -> tuple[str | None, str | None]:
    urls = re.findall(r"https?://[^\s]+", output or "")
    clean = [_safe_url(url.rstrip(".,)")) for url in urls]
    clean = [url for url in clean if url]
    local = next((url for url in clean if url.startswith("http://127.0.0.1") or url.startswith("http://localhost")), None)
    remote = next((url for url in clean if url.startswith("https://")), None)
    override = _safe_url(os.environ.get("CONVEYOR_HANDOFF_WEB_URL", ""))
    return override or remote, local


def _script_path() -> Path:
    return Path(__file__).resolve().parent / "scripts" / "novnc_handoff.sh"


def _run_transport(command: str) -> tuple[bool, str, str | None, str | None]:
    if command not in {"start", "stop"}:
        raise ValueError("invalid handoff transport command")
    script = _script_path()
    if not script.is_file():
        return False, "novnc handoff helper is missing", None, None
    try:
        result = subprocess.run(
            ["/usr/bin/env", "bash", str(script), command],
            cwd=str(script.parent.parent),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"handoff transport {command} failed: {type(exc).__name__}", None, None
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    remote, local = _extract_urls(combined)
    if result.returncode != 0:
        message = " ".join(combined.split())[-500:] or f"handoff transport {command} exited {result.returncode}"
        return False, message, remote, local
    return True, "", remote, local


def _write_status(settings: Any, *, phase: str, running: bool, ready: bool,
                  url: str | None = None, local_url: str | None = None,
                  error: str | None = None) -> None:
    path = sidecar_status_path(settings)
    value = {
        "phase": phase[:32],
        "running": bool(running),
        "ready": bool(ready),
        "url": _safe_url(url or ""),
        "local_url": _safe_url(local_url or ""),
        "error": (" ".join(str(error or "").split())[:500] or None),
        "updated_at": time.time(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    old_umask = os.umask(0o077)
    try:
        tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def run_once(settings: Any) -> None:
    store = HumanTakeoverStore(settings)
    current = store.current()
    close = read_close_request(settings)
    previous = read_sidecar_status(settings)

    if close and (not current or close.get("session_id") != current.get("id")):
        clear_close_request(settings, session_id=str(close.get("session_id") or ""))
        close = None

    if close and current:
        action = str(close.get("action"))
        _write_status(
            settings, phase="closing", running=transport_running(), ready=False,
            url=previous.get("url"), local_url=previous.get("local_url"),
        )
        ok, error, _, _ = _run_transport("stop")
        if not ok or transport_running():
            _write_status(
                settings, phase="closing", running=transport_running(), ready=False,
                url=previous.get("url"), local_url=previous.get("local_url"),
                error=error or "handoff transport is still running",
            )
            return
        result = store.complete(str(current["id"])) if action == "complete" else store.cancel(str(current["id"]))
        if result is None:
            _write_status(settings, phase="error", running=False, ready=False, error="takeover close transition failed")
            return
        clear_close_request(settings, session_id=str(current["id"]))
        _write_status(settings, phase=str(result.get("state") or action), running=False, ready=False)
        return

    if current:
        if transport_running():
            _write_status(
                settings, phase="ready", running=True, ready=True,
                url=previous.get("url"), local_url=previous.get("local_url") or "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale",
            )
            return
        _write_status(settings, phase="starting", running=False, ready=False)
        ok, error, remote, local = _run_transport("start")
        running = transport_running()
        if not ok or not running:
            _write_status(
                settings, phase="error", running=running, ready=False,
                url=remote, local_url=local, error=error or "handoff transport did not remain running",
            )
            return
        _write_status(
            settings, phase="ready", running=True, ready=True,
            url=remote, local_url=local or "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale",
        )
        return

    if transport_running():
        ok, error, _, _ = _run_transport("stop")
        if not ok or transport_running():
            _write_status(settings, phase="cleanup_error", running=transport_running(), ready=False, error=error)
            return
    clear_close_request(settings)
    _write_status(settings, phase="idle", running=False, ready=False)


def _handle_signal(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    settings = load_settings()
    while not _STOP:
        try:
            run_once(settings)
        except Exception as exc:
            _write_status(
                settings,
                phase="error",
                running=transport_running(),
                ready=False,
                error=f"sidecar error: {type(exc).__name__}",
            )
        time.sleep(POLL_SECONDS)

    # A service stop must close any transport, but it deliberately does not
    # mark an active takeover completed.  The lease keeps automation paused.
    if transport_running():
        _run_transport("stop")
    _write_status(settings, phase="stopped", running=transport_running(), ready=False)


if __name__ == "__main__":
    main()
