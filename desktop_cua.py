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
from desktop_computer_requests import (
    action_enforces_app_allowlist,
    check_app_allowlist_blocklist,
    redact_computer_action,
    validate_ax_fields,
)

def get_active_app_macos() -> str:
    """Return the frontmost application name (best-effort)."""
    try:
        cmd = [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            name = (proc.stdout or "").strip()
            if name:
                return name
    except Exception:
        pass
    return "Unknown"


def get_app_name_for_pid(pid: int) -> str | None:
    """Resolve macOS process name for a unix pid (best-effort).

    Used for AX-targeted actions so allowlist/blocklist checks the *target*
    app (e.g. Calculator) instead of whatever is currently frontmost
    (often Codex / Terminal while the agent is driving).
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    try:
        cmd = [
            "osascript",
            "-e",
            f'tell application "System Events" to get name of first process whose unix id is {pid_int}',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            name = (proc.stdout or "").strip()
            if name:
                return name
    except Exception:
        pass
    return None


def resolve_app_for_action(action: dict) -> str:
    """App name used for allowlist/blocklist for this action.

    Prefer the AX target pid's process name when present; otherwise the
    frontmost app. This prevents hands-free AX clicks on an allowed app
    from being rejected because Codex/Terminal is frontmost.
    """
    if not isinstance(action, dict):
        return get_active_app_macos()
    pid = action.get("pid")
    if pid is not None:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = 0
        if pid_int > 0:
            name = get_app_name_for_pid(pid_int)
            if name:
                return name
            # Pid was specified but unresolvable — do not fall back to a
            # different frontmost app (that reintroduces the false block).
            return "Unknown"
    return get_active_app_macos()

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
        # A task may name its target app. Resolve and activate it locally so
        # observation and AX hints come from the intended window, even when
        # the agent itself is currently frontmost.
        target_error = self._prepare_target_app(action)
        if target_error:
            return {
                "result_ok": False,
                "error": target_error,
                "action_type": action.get("action"),
                "node_id": node_id,
            }
        # Target app (pid) for AX actions; frontmost otherwise.
        # Observe/wait: blocklist only — allowlist would kill planner's first
        # observe while Codex is frontmost under Calculator-only allowlist.
        active_app = resolve_app_for_action(action)
        act_name = action.get("action") if isinstance(action, dict) else None
        # With a non-empty app allowlist, bare x/y clicks cannot prove they
        # target an allowed app (frontmost is often Codex). Require AX.
        allowed_apps = getattr(self.settings, "conveyor_computer_allowed_apps", ()) or ()
        if (
            act_name == "click"
            and allowed_apps
            and not any(
                action.get(k) is not None
                for k in ("pid", "window_id", "element_index", "element_token")
            )
        ):
            return {
                "result_ok": False,
                "error": "ax_required_when_app_allowlist_set",
                "action_type": "click",
                "node_id": node_id,
                "active_app": active_app,
            }
        is_ok, reason = check_app_allowlist_blocklist(
            self.settings,
            active_app,
            enforce_allowlist=action_enforces_app_allowlist(action),
        )
        if not is_ok:
            return {
                "result_ok": False,
                "error": reason,
                "action_type": action.get("action"),
                "node_id": node_id,
                "active_app": active_app,
            }
        # Log only the redacted action — never the raw payload.
        logger.info("cua execute (redacted): %s", json.dumps(redact_computer_action(action)))
        act = action.get("action")
        if act == "wait":
            res = self._wait(action, node_id)
        elif act == "observe":
            res = self._observe(action, node_id)
        elif act == "click":
            res = self._click(action, node_id)
        elif act == "type":
            res = self._type_text(action, node_id)
        elif act == "hotkey":
            res = self._hotkey(action, node_id)
        elif act == "scroll":
            res = self._scroll(action, node_id)
        else:
            res = {
                "result_ok": False,
                "error": "unknown_action",
                "action_type": act,
                "node_id": node_id,
            }
        res["active_app"] = active_app
        return res

    def _prepare_target_app(self, action: dict) -> str | None:
        """Resolve ``target_app`` to a running pid and foreground it.

        This is deliberately local and metadata-only. It does not launch an
        app or inspect window titles; an absent app is reported clearly so
        the planner/operator can open it and retry.
        """
        if not isinstance(action, dict) or not action.get("target_app"):
            return None
        if action.get("pid") is not None:
            return None
        wanted = str(action.get("target_app") or "").strip().lower()
        if not wanted or len(wanted) > 128:
            return "invalid_target_app"
        listed = self._call_tool("list_apps", {}, timeout=20)
        if not listed.get("ok"):
            return "target_app_list_failed"
        data = listed.get("data") or {}
        apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(apps, list):
            return "target_app_list_invalid"
        matches = []
        for app in apps:
            if not isinstance(app, dict):
                continue
            name = str(app.get("name") or app.get("app_name") or "").strip()
            if name.lower() == wanted:
                matches.append(app)
        if not matches:
            return "target_app_not_found"
        app = next((item for item in matches if item.get("running")), matches[0])
        if not app.get("running") or not app.get("pid"):
            return "target_app_not_running"
        try:
            pid = int(app["pid"])
        except (TypeError, ValueError):
            return "target_app_pid_invalid"
        activated = self._call_tool("bring_to_front", {"pid": pid}, timeout=20)
        if not activated.get("ok"):
            return "target_app_activate_failed"
        action["pid"] = pid
        return None

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
                res = _observe_result_from_meta(meta, node_id)
                return self._attach_ax_hints(res, action)

        meta = self._observe_desktop_state(node_id)
        res = _observe_result_from_meta(meta, node_id)
        return self._attach_ax_hints(res, action)

    def _attach_ax_hints(self, res: dict, action: dict) -> dict:
        """Merge best-effort AX window/element hints into an observe result.

        Without this, CodexPlanner only sees a screenshot id and falls back
        to bare x/y clicks, which then fail under app allowlists when Codex
        (not Calculator) is frontmost.
        """
        if not res.get("result_ok"):
            return res
        try:
            hints = self._collect_ax_hints(action)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("ax hint collection failed: %s", exc)
            return res
        if hints:
            res.update(hints)
        return res

    def _collect_ax_hints(self, action: dict | None = None) -> dict[str, Any]:
        """Pick a target app window and return pid/window_id/element_hints.

        Priority:
        1. action.pid / action.window_id if provided
        2. CONVEYOR_COMPUTER_ALLOWED_APPS (single or matching)
        3. frontmost app
        """
        action = action if isinstance(action, dict) else {}
        preferred_pid = None
        preferred_wid = None
        if action.get("pid") is not None:
            try:
                preferred_pid = int(action["pid"])
            except (TypeError, ValueError):
                preferred_pid = None
        if action.get("window_id") is not None:
            try:
                preferred_wid = int(action["window_id"])
            except (TypeError, ValueError):
                preferred_wid = None

        allowed = tuple(
            a.strip()
            for a in (getattr(self.settings, "conveyor_computer_allowed_apps", ()) or ())
            if str(a).strip()
        )
        target_names = [a.lower() for a in allowed]
        front = get_active_app_macos()

        windows: list[dict] = []
        # Prefer unfiltered list; fall back to per-pid if needed.
        listed = self._call_tool("list_windows", {}, timeout=20)
        if listed.get("ok") and isinstance(listed.get("data"), dict):
            raw = listed["data"].get("windows") or []
            if isinstance(raw, list):
                windows = [w for w in raw if isinstance(w, dict)]
        if not windows and preferred_pid:
            listed = self._call_tool("list_windows", {"pid": preferred_pid}, timeout=20)
            if listed.get("ok") and isinstance(listed.get("data"), dict):
                raw = listed["data"].get("windows") or []
                if isinstance(raw, list):
                    windows = [w for w in raw if isinstance(w, dict)]

        def _area(w: dict) -> float:
            b = w.get("bounds") or {}
            try:
                return float(b.get("width") or 0) * float(b.get("height") or 0)
            except (TypeError, ValueError):
                return 0.0

        def _match_app(w: dict, names: list[str]) -> bool:
            app = str(w.get("app_name") or "").strip().lower()
            return bool(app) and app in names

        candidates = list(windows)
        if preferred_pid:
            candidates = [
                w for w in candidates
                if int(w.get("pid") or 0) == preferred_pid
            ] or candidates
        if target_names:
            filtered = [w for w in candidates if _match_app(w, target_names)]
            if filtered:
                candidates = filtered
        elif front and front != "Unknown":
            filtered = [w for w in candidates if _match_app(w, [front.lower()])]
            if filtered:
                candidates = filtered

        # Prefer large on-screen windows (skip menu-bar strips).
        candidates = sorted(
            candidates,
            key=lambda w: (
                1 if w.get("is_on_screen") else 0,
                1 if _area(w) >= 5000 else 0,
                _area(w),
            ),
            reverse=True,
        )
        if preferred_wid is not None:
            prefer = [w for w in candidates if int(w.get("window_id") or -1) == preferred_wid]
            if prefer:
                candidates = prefer + [w for w in candidates if w not in prefer]

        chosen = None
        for w in candidates:
            if _area(w) < 5000 and preferred_wid is None:
                continue
            try:
                pid = int(w.get("pid"))
                wid = int(w.get("window_id"))
            except (TypeError, ValueError):
                continue
            state = self._call_tool(
                "get_window_state", {"pid": pid, "window_id": wid}, timeout=30,
            )
            if not state.get("ok"):
                continue
            data = state.get("data") or {}
            elems = data.get("elements") if isinstance(data, dict) else None
            if not isinstance(elems, list) or not elems:
                continue
            hints = _extract_element_hints(elems)
            if not hints and preferred_wid is None:
                # Still accept window even without short-labeled buttons.
                hints = _extract_element_hints(elems, loose=True)
            chosen = {
                "pid": pid,
                "window_id": wid,
                "ax_app": str(w.get("app_name") or "")[:128] or None,
                "element_hints": hints,
            }
            if hints:
                break
        if not chosen:
            return {}
        out: dict[str, Any] = {
            "pid": chosen["pid"],
            "window_id": chosen["window_id"],
        }
        if chosen.get("ax_app"):
            out["ax_app"] = chosen["ax_app"]
        if chosen.get("element_hints"):
            out["element_hints"] = chosen["element_hints"]
        return out

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
        # Validate AX fields first
        ok, err = validate_ax_fields(action)
        if not ok:
            return {"result_ok": False, "error": f"validation_error:{err}", "action_type": "click", "node_id": node_id}
            
        has_ax = any(action.get(k) is not None for k in ("pid", "window_id", "element_index"))
        has_xy = "x" in action and "y" in action
        
        if not has_ax and not has_xy and "element_token" not in action:
            return {"result_ok": False, "error": "click_target_required", "action_type": "click", "node_id": node_id}
            
        result = None
        used_method = None
        
        if has_ax:
            # AX/action-based click
            ax_args: dict[str, Any] = {}
            _copy_optional(
                action,
                ax_args,
                (
                    "pid", "window_id", "element_index", "element_token",
                    "delivery_mode", "scope", "button",
                ),
            )
            # Ensure integer types
            for k in ("pid", "window_id", "element_index"):
                if ax_args.get(k) is not None:
                    ax_args[k] = int(ax_args[k])

            # Populate Cua element cache before index-based click.
            if ax_args.get("pid") is not None and ax_args.get("window_id") is not None:
                self._call_tool(
                    "get_window_state",
                    {"pid": ax_args["pid"], "window_id": ax_args["window_id"]},
                    timeout=30,
                )

            if "pid" not in ax_args and "window_id" not in ax_args and "scope" not in ax_args:
                ax_args["scope"] = "desktop"
            if ax_args.get("scope") == "desktop":
                self._call_tool("set_config", {"capture_scope": "desktop"}, timeout=15)
                
            called = self._call_tool("click", ax_args)
            res = _result_from_call(called, "click", node_id)
            if res.get("result_ok"):
                result = res
                used_method = "ax_click"
                logger.info("ax_click succeeded: %s", json.dumps(redact_computer_action(action)))
            else:
                logger.info("ax_click failed, error=%s", res.get("error"))
                if has_xy:
                    logger.info("ax_click failed; falling back to xy_click")
                else:
                    result = res
                    used_method = "ax_click"
                    
        if result is None and has_xy:
            # Use x/y click as fallback
            xy_args: dict[str, Any] = {}
            _copy_optional(
                action,
                xy_args,
                (
                    "pid", "window_id", "element_index", "element_token",
                    "delivery_mode", "scope", "button",
                ),
            )
            # Ensure integer types if present
            for k in ("pid", "window_id", "element_index"):
                if xy_args.get(k) is not None:
                    xy_args[k] = int(xy_args[k])
                    
            ok_coord, x, y = _xy(action)
            if not ok_coord:
                return {"result_ok": False, "error": "bad_coords", "action_type": "click", "node_id": node_id}
            xy_args["x"] = x
            xy_args["y"] = y
            
            if "pid" not in xy_args and "window_id" not in xy_args and "scope" not in xy_args:
                xy_args["scope"] = "desktop"
            if xy_args.get("scope") == "desktop":
                self._call_tool("set_config", {"capture_scope": "desktop"}, timeout=15)
                
            called = self._call_tool("click", xy_args)
            result = _result_from_call(called, "click", node_id)
            used_method = "xy_click"
            logger.info("xy_click executed, result_ok=%s", result.get("result_ok"))
            
        if result is None:
            # Fallback when neither executed or both failed
            result = {"result_ok": False, "error": "click_failed", "action_type": "click", "node_id": node_id}
            used_method = "ax_click" if has_ax else "xy_click"
            
        result["click_method"] = used_method
        logger.info("Click method used: %s", used_method)
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


def _extract_element_hints(elems: list, *, loose: bool = False) -> list[dict[str, Any]]:
    """Build short, allow-list-safe AX element hints for the planner.

    Prefers buttons / short labels (digits, single words). Long free text
    is dropped so results never carry raw window content.
    """
    hints: list[dict[str, Any]] = []
    seen: set[int] = set()
    for e in elems:
        if not isinstance(e, dict):
            continue
        try:
            idx = int(e.get("element_index"))
        except (TypeError, ValueError):
            continue
        if idx in seen:
            continue
        role = str(e.get("role") or "")
        label = e.get("label")
        label_s = label.strip() if isinstance(label, str) else ""
        role_l = role.lower()
        is_button = "button" in role_l
        if not loose:
            if not is_button:
                continue
            if not label_s or len(label_s) > 32:
                continue
        else:
            if not label_s or len(label_s) > 32:
                continue
        seen.add(idx)
        hint: dict[str, Any] = {"element_index": idx, "role": role[:48] if role else "AXUnknown"}
        if label_s:
            hint["label"] = label_s[:32]
        token = e.get("element_token")
        if isinstance(token, str) and token.strip():
            hint["element_token"] = token.strip()[:64]
        hints.append(hint)
        if len(hints) >= 40:
            break
    return hints


class FakeCuaTransport(CuaTransport):
    """Deterministic, network-free backend for smokes and local tests.

    Models a single 1920x1080 display. ``observe`` returns a stable
    fake screenshot id/hash; mutating actions are recorded only. Typed
    text is never retained (redaction is enforced by the caller, and
    this transport ignores any raw payload entirely).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.screen = (1920, 1080)
        self.last_pointer: tuple[int, int] = (0, 0)
        self.log: list[dict] = []
        self.fake_screenshot_seq = 0
        self.mock_active_app = "Finder"

    def execute(self, action: dict, node_id: str) -> dict:
        act = action.get("action")
        redacted = redact_computer_action(action)
        self.log.append({"action": act, "redacted": redacted})
        
        # Mirror LocalCuaTransport: pid-targeted actions use the target
        # app (mock or single allowlist entry), not frontmost mock_active_app.
        if action.get("pid") is not None and action.get("_mock_target_app"):
            active_app = str(action.get("_mock_target_app") or "Unknown")
        elif action.get("pid") is not None:
            apps = tuple(
                a for a in (getattr(self.settings, "conveyor_computer_allowed_apps", ()) or ())
                if str(a).strip()
            )
            if len(apps) == 1:
                active_app = apps[0]
            else:
                active_app = (
                    action.get("_mock_active_app")
                    or getattr(self, "mock_active_app", "Finder")
                )
        else:
            active_app = action.get("_mock_active_app") or getattr(self, "mock_active_app", "Finder")
        settings = getattr(self, "settings", None)
        if settings:
            allowed_apps = getattr(settings, "conveyor_computer_allowed_apps", ()) or ()
            if (
                act == "click"
                and allowed_apps
                and not any(
                    action.get(k) is not None
                    for k in ("pid", "window_id", "element_index", "element_token")
                )
            ):
                return {
                    "result_ok": False,
                    "error": "ax_required_when_app_allowlist_set",
                    "action_type": "click",
                    "node_id": node_id,
                    "active_app": active_app,
                }
            is_ok, reason = check_app_allowlist_blocklist(
                settings,
                active_app,
                enforce_allowlist=action_enforces_app_allowlist(action),
            )
            if not is_ok:
                return {
                    "result_ok": False,
                    "error": reason,
                    "action_type": act,
                    "node_id": node_id,
                    "active_app": active_app,
                }
                
        if act == "observe":
            self.fake_screenshot_seq += 1
            sid = f"fake_obs_{self.fake_screenshot_seq}"
            import hashlib
            sha = hashlib.sha256(sid.encode("utf-8")).hexdigest()
            res = {
                "result_ok": True,
                "action_type": "observe",
                "screenshot_id": sid,
                "sha256": sha,
                "width": self.screen[0],
                "height": self.screen[1],
                "obs_text_len": 0,
                "node_id": node_id,
            }
            # Inject AX hints for smokes / planner (mirrors LocalCuaTransport).
            mock_hints = action.get("_mock_element_hints")
            if isinstance(mock_hints, list) and mock_hints:
                res["element_hints"] = mock_hints
            if action.get("_mock_pid") is not None:
                res["pid"] = int(action["_mock_pid"])
            if action.get("_mock_window_id") is not None:
                res["window_id"] = int(action["_mock_window_id"])
            if action.get("_mock_ax_app"):
                res["ax_app"] = str(action["_mock_ax_app"])[:128]
            elif getattr(self.settings, "conveyor_computer_allowed_apps", None):
                apps = getattr(self.settings, "conveyor_computer_allowed_apps", ()) or ()
                if len(apps) == 1:
                    res.setdefault("ax_app", apps[0])
                    res.setdefault("pid", 59084)
                    res.setdefault("window_id", 7235)
                    res.setdefault("element_hints", [
                        {"element_index": 13, "role": "AXButton", "label": "1"},
                        {"element_index": 14, "role": "AXButton", "label": "2"},
                    ])
        elif act == "click":
            # Validation
            ok, err = validate_ax_fields(action)
            if not ok:
                return {"result_ok": False, "error": f"validation_error:{err}", "action_type": "click", "node_id": node_id, "active_app": active_app}
                
            has_ax = any(action.get(k) is not None for k in ("pid", "window_id", "element_index"))
            has_xy = "x" in action and "y" in action
            
            if not has_ax and not has_xy and "element_token" not in action:
                return {"result_ok": False, "error": "click_target_required", "action_type": "click", "node_id": node_id, "active_app": active_app}
                
            used_method = "ax_click" if has_ax else "xy_click"
            if not has_ax and has_xy:
                try:
                    int(action.get("x", 0))
                    int(action.get("y", 0))
                except (TypeError, ValueError):
                    return {"result_ok": False, "error": "bad_coords", "action_type": "click", "node_id": node_id, "active_app": active_app}
            
            self.last_pointer = (int(action.get("x", 0)), int(action.get("y", 0))) if has_xy else (0, 0)
            res = {
                "result_ok": True, 
                "action_type": "click", 
                "node_id": node_id,
                "click_method": used_method,
            }
        elif act == "type":
            text_len = len(action.get("text", "") or "")
            res = {"result_ok": True, "action_type": "type", "obs_text_len": text_len, "node_id": node_id}
        elif act == "hotkey":
            keys_len = len(action.get("keys", []) or [])
            res = {"result_ok": True, "action_type": "hotkey", "keys_len": keys_len, "node_id": node_id}
        elif act == "scroll":
            res = {"result_ok": True, "action_type": "scroll", "node_id": node_id}
        elif act == "wait":
            seconds = max(0, min(5, float(action.get("seconds", 0) or 0)))
            time.sleep(seconds)
            res = {"result_ok": True, "action_type": "wait", "node_id": node_id}
        else:
            res = {"result_ok": False, "error": "unknown_action", "action_type": act, "node_id": node_id}
            
        res["active_app"] = active_app
        return res


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
        else:
            self.transport.settings = self.settings

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
            "keys_len", "text_len", "click_method", "active_app",
            "pid", "window_id", "ax_app", "element_hints",
        }
        return {k: v for k, v in result.items() if k in allowed}


def build_driver(settings: Settings, *, fake: bool = False, node_id: str = "") -> CuaDriver:
    """Construct a CuaDriver, optionally with the fake transport."""
    driver = CuaDriver(settings=settings, node_id=node_id)
    if fake:
        driver.transport = FakeCuaTransport(settings=settings)
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
