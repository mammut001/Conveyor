"""desktop_cua.py — local Cua backend for the Mac desktop agent (P5.6).

This module runs ONLY on the operator's Mac, inside ``desktop_agent.py``.
It is the single place that talks to ``cua-driver`` (trycua/cua). The
VPS never receives Cua output and never reaches the driver over the
network — all execution stays local to the Mac (see
docs/desktop_security.md and the task brief).

Design:
- ``probe_cua_driver`` checks the driver binary exists before use.
- ``CuaDriver.status`` returns metadata ONLY (no screen content).
- ``CuaDriver.execute`` dispatches one action at a time:
  observe / click / type / hotkey / scroll / wait.
- The real transport uses the local ``cua-driver call`` CLI wrapper.
  ``cua-driver mcp`` remains the default config because it is the
  official MCP registration command, but Conveyor does not expose MCP
  over the network.
- Typed text and hotkey payloads are REDACTED in every log line.
- ``FakeCuaTransport`` provides a deterministic, network-free backend
  used by the smoke suite (``CONVEYOR_COMPUTER_BACKEND=fake`` style
  tests) and by ``desktop_computer_requests``-level loop tests.
"""
from __future__ import annotations

import json
import logging
import base64
import hashlib
import shutil
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from config import Settings
from desktop_computer_requests import redact_computer_action

logger = logging.getLogger("desktop_cua")


# ---- driver probe -----------------------------------------------------------

def _driver_binary(cmd: str) -> str:
    """First token of the driver command (the executable name/path)."""
    parts = shlex.split(cmd or "cua-driver mcp")
    return parts[0] if parts else "cua-driver"


def probe_cua_driver(cmd: str | None = None, *, settings: Settings | None = None) -> dict:
    """Probe whether the Cua driver is available on this Mac.

    Returns metadata-only: {available, path, version, error}.
    Never raises; missing driver is an expected, reported state.
    """
    if cmd is None and settings is not None:
        cmd = settings.conveyor_cua_driver_cmd or "cua-driver mcp"
    cmd = cmd or "cua-driver mcp"
    binary = _driver_binary(cmd)
    path = shutil.which(binary)
    if not path:
        return {
            "available": False,
            "path": None,
            "version": None,
            "error": f"driver_not_found:{binary}",
        }
    # Best-effort version probe; failure is not fatal.
    version = None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            version = (proc.stdout or proc.stderr).strip().splitlines()[0][:64] or None
    except Exception:
        version = None
    permissions = None
    try:
        proc = subprocess.run(
            [path, "permissions", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            parsed = json.loads(proc.stdout or "{}")
            if isinstance(parsed, dict):
                source = parsed.get("source") if isinstance(parsed.get("source"), dict) else {}
                status = parsed.get("status")
                if status is None and parsed.get("accessibility") is True and parsed.get("screen_recording") is True:
                    status = "granted"
                daemon_running = parsed.get("daemon_running")
                if daemon_running is None and source.get("attribution") == "driver-daemon":
                    daemon_running = True
                permissions = {
                    "status": status,
                    "daemon_running": daemon_running,
                    "accessibility": parsed.get("accessibility"),
                    "screen_recording": parsed.get("screen_recording"),
                    "screen_recording_capturable": parsed.get("screen_recording_capturable"),
                    "reason": parsed.get("reason"),
                }
    except Exception:
        permissions = None
    return {
        "available": True,
        "path": path,
        "version": version,
        "permissions": permissions,
        "error": None,
    }


# ---- transports -------------------------------------------------------------

class CuaTransport:
    """Abstraction over how an action reaches the real driver."""

    def execute(self, action: dict, node_id: str) -> dict:
        raise NotImplementedError


class LocalCuaTransport(CuaTransport):
    """Real transport: shells out to the local ``cua-driver`` CLI wrapper.

    ``cua-driver mcp`` is an MCP stdio server, not a one-shot JSON CLI.
    The same binary also exposes ``cua-driver call <tool> [json]``, which
    is the local wrapper path used here. This keeps Cua off the network
    and behind a single local binary. Inactive / unavailable driver
    degrades gracefully to a structured error the agent can report back.
    """

    def __init__(self, cmd: str, *, settings: Settings, timeout_seconds: int = 120):
        self.cmd = cmd or "cua-driver mcp"
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.binary = _driver_binary(self.cmd)

    def execute(self, action: dict, node_id: str) -> dict:
        probe = probe_cua_driver(self.cmd)
        if not probe.get("available"):
            return {
                "result_ok": False,
                "error": probe.get("error", "driver_unavailable"),
                "action_type": action.get("action"),
                "node_id": node_id,
            }
        # Log only the redacted action — never the raw payload.
        logger.info("cua execute (redacted): %s", json.dumps(redact_computer_action(action)))
        act = action.get("action")
        if act == "wait":
            return self._wait(action, node_id)
        if act == "observe":
            return self._observe(action, node_id)
        if act == "click":
            return self._click(action, node_id)
        if act == "type":
            return self._type_text(action, node_id)
        if act == "hotkey":
            return self._hotkey(action, node_id)
        if act == "scroll":
            return self._scroll(action, node_id)
        return {
            "result_ok": False,
            "error": "unknown_action",
            "action_type": act,
            "node_id": node_id,
        }

    def _call_tool(self, tool: str, args: dict | None = None, *, timeout: int | None = None) -> dict:
        cmd = [self.binary, "call", tool]
        if args:
            cmd.append(json.dumps(args, ensure_ascii=False))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "driver_timeout"}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": f"driver_error:{type(exc).__name__}"}
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            suffix = f":{err[0][:120]}" if err else ""
            return {"ok": False, "error": f"driver_exit_{proc.returncode}{suffix}"}
        try:
            out = json.loads(proc.stdout or "{}")
        except Exception:
            out = {"raw_stdout_len": len(proc.stdout or "")}
        if isinstance(out, dict) and out.get("isError") is True:
            return {"ok": False, "error": _safe_error_from_tool_result(out)}
        return {"ok": True, "data": out if isinstance(out, dict) else {}}

    def _observe(self, action: dict, node_id: str) -> dict:
        called = self._call_tool("screenshot", timeout=60)
        if called.get("ok"):
            data = called.get("data") or {}
            png_b64 = _extract_first_string(data, ("screenshot_png_b64", "image_png", "data"))
            if png_b64:
                try:
                    png = base64.b64decode(png_b64, validate=True)
                except Exception:
                    return {
                        "result_ok": False,
                        "error": "screenshot_bad_base64",
                        "action_type": "observe",
                        "node_id": node_id,
                    }
                meta = _save_cua_screenshot(self.settings, png, node_id=node_id)
                return _observe_result_from_meta(meta, node_id)

        meta = self._observe_desktop_state(node_id)
        return _observe_result_from_meta(meta, node_id)

    def _observe_desktop_state(self, node_id: str) -> dict:
        prepared = _prepare_cua_screenshot_path(self.settings)
        if not prepared.get("ok"):
            return prepared
        self._call_tool("set_config", {"capture_scope": "desktop"}, timeout=15)
        called = self._call_tool(
            "get_desktop_state",
            {"screenshot_out_file": prepared["path"]},
            timeout=60,
        )
        if not called.get("ok"):
            return {"ok": False, "error": called.get("error", "get_desktop_state_failed")}
        return _record_existing_cua_screenshot(
            self.settings,
            Path(prepared["path"]),
            prepared["screenshot_id"],
            node_id=node_id,
        )

    def _click(self, action: dict, node_id: str) -> dict:
        args: dict[str, Any] = {}
        _copy_optional(
            action,
            args,
            (
                "pid", "window_id", "element_index", "element_token",
                "delivery_mode", "scope", "button",
            ),
        )
        if "x" in action or "y" in action:
            ok, x, y = _xy(action)
            if not ok:
                return {"result_ok": False, "error": "bad_coords", "action_type": "click", "node_id": node_id}
            args["x"] = x
            args["y"] = y
        if not any(k in args for k in ("x", "y", "element_index", "element_token")):
            return {"result_ok": False, "error": "click_target_required", "action_type": "click", "node_id": node_id}
        if "pid" not in args and "window_id" not in args and "scope" not in args:
            args["scope"] = "desktop"
        if args.get("scope") == "desktop":
            # Desktop-scope pixel click is gated by the driver's capture_scope.
            self._call_tool("set_config", {"capture_scope": "desktop"}, timeout=15)
        called = self._call_tool("click", args)
        result = _result_from_call(called, "click", node_id)
        data = called.get("data") if isinstance(called, dict) else None
        if isinstance(data, dict):
            for key in ("effect", "path", "verified"):
                if key in data:
                    result[key] = data[key]
        return result

    def _type_text(self, action: dict, node_id: str) -> dict:
        pid = action.get("pid")
        if pid is None:
            return {
                "result_ok": False,
                "error": "pid_required_for_type_text",
                "action_type": "type",
                "node_id": node_id,
            }
        args = {"pid": int(pid), "text": str(action.get("text", ""))}
        _copy_optional(action, args, ("window_id", "element_index", "element_token", "x", "y", "delivery_mode"))
        called = self._call_tool("type_text", args)
        result = _result_from_call(called, "type", node_id)
        if result.get("result_ok"):
            result["text_len"] = len(action.get("text", "") or "")
        return result

    def _hotkey(self, action: dict, node_id: str) -> dict:
        pid = action.get("pid")
        if pid is None:
            return {
                "result_ok": False,
                "error": "pid_required_for_hotkey",
                "action_type": "hotkey",
                "node_id": node_id,
            }
        keys = action.get("keys")
        if not isinstance(keys, list) or not keys:
            return {"result_ok": False, "error": "bad_keys", "action_type": "hotkey", "node_id": node_id}
        args = {"pid": int(pid), "keys": [str(k) for k in keys]}
        _copy_optional(action, args, ("window_id", "x", "y", "delivery_mode"))
        called = self._call_tool("hotkey", args)
        result = _result_from_call(called, "hotkey", node_id)
        if result.get("result_ok"):
            result["keys_len"] = len(keys)
        return result

    def _scroll(self, action: dict, node_id: str) -> dict:
        pid = action.get("pid")
        if pid is None:
            return {
                "result_ok": False,
                "error": "pid_required_for_scroll",
                "action_type": "scroll",
                "node_id": node_id,
            }
        dx = float(action.get("dx", 0) or 0)
        dy = float(action.get("dy", 0) or 0)
        if abs(dx) > abs(dy):
            direction = "right" if dx > 0 else "left"
            amount = max(1, min(50, int(abs(dx) / 120) or 1))
        else:
            direction = "down" if dy < 0 else "up"
            amount = max(1, min(50, int(abs(dy) / 120) or 1))
        args = {"pid": int(pid), "direction": direction, "amount": amount, "by": "line"}
        _copy_optional(action, args, ("window_id", "element_index", "element_token", "x", "y", "delivery_mode"))
        called = self._call_tool("scroll", args)
        return _result_from_call(called, "scroll", node_id)

    def _wait(self, action: dict, node_id: str) -> dict:
        seconds = max(0, min(30, float(action.get("seconds", 0) or 0)))
        time.sleep(seconds)
        return {"result_ok": True, "action_type": "wait", "node_id": node_id}


def _observe_result_from_meta(meta: dict, node_id: str) -> dict:
    if not meta.get("ok"):
        return {
            "result_ok": False,
            "error": meta.get("error", "screenshot_save_failed"),
            "action_type": "observe",
            "node_id": node_id,
        }
    return {
        "result_ok": True,
        "action_type": "observe",
        "screenshot_id": meta.get("screenshot_id"),
        "sha256": meta.get("sha256"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "node_id": node_id,
    }


class FakeCuaTransport(CuaTransport):
    """Deterministic, network-free backend for smokes and local tests.

    Models a single 1920x1080 display. ``observe`` returns a stable
    fake screenshot id/hash; mutating actions are recorded only. Typed
    text is never retained (redaction is enforced by the caller, and
    this transport ignores any raw payload entirely).
    """

    def __init__(self) -> None:
        self.screen = (1920, 1080)
        self.last_pointer: tuple[int, int] = (0, 0)
        self.log: list[dict] = []
        self.fake_screenshot_seq = 0

    def execute(self, action: dict, node_id: str) -> dict:
        act = action.get("action")
        redacted = redact_computer_action(action)
        # Record only redacted metadata — never raw text/keys.
        self.log.append({"action": act, "redacted": redacted})
        if act == "observe":
            self.fake_screenshot_seq += 1
            sid = f"fake_obs_{self.fake_screenshot_seq}"
            import hashlib
            sha = hashlib.sha256(sid.encode("utf-8")).hexdigest()
            return {
                "result_ok": True,
                "action_type": "observe",
                "screenshot_id": sid,
                "sha256": sha,
                "width": self.screen[0],
                "height": self.screen[1],
                "obs_text_len": 0,
                "node_id": node_id,
            }
        if act == "click":
            try:
                x = int(action.get("x", 0))
                y = int(action.get("y", 0))
            except (TypeError, ValueError):
                return {"result_ok": False, "error": "bad_coords", "action_type": "click", "node_id": node_id}
            self.last_pointer = (x, y)
            return {"result_ok": True, "action_type": "click", "node_id": node_id}
        if act == "type":
            # Raw text intentionally dropped; only length is reflected.
            text_len = len(action.get("text", "") or "")
            return {"result_ok": True, "action_type": "type", "obs_text_len": text_len, "node_id": node_id}
        if act == "hotkey":
            keys_len = len(action.get("keys", []) or [])
            return {"result_ok": True, "action_type": "hotkey", "keys_len": keys_len, "node_id": node_id}
        if act == "scroll":
            return {"result_ok": True, "action_type": "scroll", "node_id": node_id}
        if act == "wait":
            seconds = max(0, min(5, float(action.get("seconds", 0) or 0)))
            time.sleep(seconds)
            return {"result_ok": True, "action_type": "wait", "node_id": node_id}
        return {"result_ok": False, "error": "unknown_action", "action_type": act, "node_id": node_id}


# ---- driver facade ----------------------------------------------------------

@dataclass
class CuaDriver:
    """High-level facade used by the desktop agent's computer poll loop."""

    settings: Settings
    node_id: str = ""
    transport: CuaTransport | None = None

    def __post_init__(self) -> None:
        if self.node_id == "":
            self.node_id = self.settings.conveyor_desktop_node_id or "macbook-payton"
        if self.transport is None:
            self.transport = LocalCuaTransport(
                self.settings.conveyor_cua_driver_cmd or "cua-driver mcp",
                settings=self.settings,
            )

    def status(self) -> dict:
        """Metadata-only status: driver availability + screen shape."""
        probe = probe_cua_driver(self.settings.conveyor_cua_driver_cmd, settings=self.settings)
        return {
            "available": bool(probe.get("available")),
            "driver_path": probe.get("path"),
            "driver_version": probe.get("version"),
            "permissions": probe.get("permissions"),
            "node_id": self.node_id,
            "mode": "local_cua",
            "note": "Cua execution stays local to this Mac; never exposed to the network.",
        }

    def execute(self, action: dict) -> dict:
        """Execute one action; always returns an allow-listed result dict."""
        if not isinstance(action, dict):
            return {"result_ok": False, "error": "bad_action", "action_type": "unknown", "node_id": self.node_id}
        act = action.get("action")
        if act not in ("observe", "click", "type", "hotkey", "scroll", "wait"):
            return {
                "result_ok": False,
                "error": "disallowed_action",
                "action_type": act,
                "node_id": self.node_id,
            }
        result = self.transport.execute(action, self.node_id)
        # Force allow-listed fields only (defence in depth before upload).
        return self._allow_list(result)

    @staticmethod
    def _allow_list(result: dict) -> dict:
        allowed = {
            "screenshot_id", "sha256", "width", "height", "obs_text_len",
            "obs_text_preview", "action_type", "action_redacted", "result_ok",
            "effect", "path", "verified", "error", "node_id", "created_at",
            "keys_len", "text_len",
        }
        return {k: v for k, v in result.items() if k in allowed}


def build_driver(settings: Settings, *, fake: bool = False, node_id: str = "") -> CuaDriver:
    """Construct a CuaDriver, optionally with the fake transport."""
    driver = CuaDriver(settings=settings, node_id=node_id)
    if fake:
        driver.transport = FakeCuaTransport()
    return driver


def _result_from_call(called: dict, action_type: str, node_id: str) -> dict:
    if not called.get("ok"):
        return {
            "result_ok": False,
            "error": called.get("error", "driver_call_failed"),
            "action_type": action_type,
            "node_id": node_id,
        }
    return {"result_ok": True, "action_type": action_type, "node_id": node_id}


def _safe_error_from_tool_result(result: dict) -> str:
    text = _extract_first_string(result, ("text", "message", "error"))
    if text:
        return text[:160]
    return "tool_error"


def _extract_first_string(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
        for item in value.values():
            found = _extract_first_string(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _extract_first_string(item, keys)
            if found:
                return found
    return None


def _copy_optional(src: dict, dest: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        if key in src and src.get(key) is not None:
            dest[key] = src[key]


def _xy(action: dict) -> tuple[bool, float, float]:
    try:
        return True, float(action.get("x")), float(action.get("y"))
    except (TypeError, ValueError):
        return False, 0.0, 0.0


def _png_dimensions(png: bytes) -> tuple[int | None, int | None]:
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None, None
    if png[12:16] != b"IHDR":
        return None, None
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    return width, height


def _prepare_cua_screenshot_path(settings: Settings) -> dict:
    from desktop_screenshot import ensure_screenshot_dir

    try:
        screenshot_dir = ensure_screenshot_dir(settings)
        screenshot_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-cua-"
            + uuid.uuid4().hex[:8]
        )
        png_path = (screenshot_dir / f"{screenshot_id}.png").resolve()
        return {"ok": True, "screenshot_id": screenshot_id, "path": str(png_path)}
    except Exception as exc:
        return {"ok": False, "error": f"screenshot_path_error:{type(exc).__name__}"}


def _record_existing_cua_screenshot(
    settings: Settings,
    png_path: Path,
    screenshot_id: str,
    *,
    node_id: str,
) -> dict:
    try:
        png = png_path.read_bytes()
        sha = hashlib.sha256(png).hexdigest()
        width, height = _png_dimensions(png)
        meta_path = png_path.with_suffix(".json")
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        metadata = {
            "ok": True,
            "screenshot_id": screenshot_id,
            "path": str(png_path),
            "metadata_path": str(meta_path),
            "sha256": sha,
            "width": width,
            "height": height,
            "display_id": "cua-driver",
            "created_at": created_at,
            "node_id": node_id,
            "helper_version": "cua-driver",
            "bytes": len(png),
        }
        tmp_path = meta_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(meta_path)
        return metadata
    except Exception as exc:
        return {"ok": False, "error": f"screenshot_record_error:{type(exc).__name__}"}


def _save_cua_screenshot(settings: Settings, png: bytes, *, node_id: str) -> dict:
    from desktop_screenshot import ensure_screenshot_dir

    try:
        screenshot_dir = ensure_screenshot_dir(settings)
        screenshot_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-cua-"
            + uuid.uuid4().hex[:8]
        )
        png_path = (screenshot_dir / f"{screenshot_id}.png").resolve()
        meta_path = (screenshot_dir / f"{screenshot_id}.json").resolve()
        png_path.write_bytes(png)
        sha = hashlib.sha256(png).hexdigest()
        width, height = _png_dimensions(png)
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        metadata = {
            "ok": True,
            "screenshot_id": screenshot_id,
            "path": str(png_path),
            "metadata_path": str(meta_path),
            "sha256": sha,
            "width": width,
            "height": height,
            "display_id": "cua-driver",
            "created_at": created_at,
            "node_id": node_id,
            "helper_version": "cua-driver",
            "bytes": len(png),
        }
        tmp_path = meta_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(meta_path)
        return metadata
    except Exception as exc:
        return {"ok": False, "error": f"screenshot_save_error:{type(exc).__name__}"}
