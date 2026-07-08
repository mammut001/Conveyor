"""desktop_computer_requests.py — P5.6 Direct Computer Use task store.

Mirrors desktop_observe_requests.py: a file-backed, cross-process
request store under ``codex_memory_root/state``. Each *task* is a
goal-driven run of the Codex action loop; each *step* is one
action the Mac desktop agent must execute via the local Cua driver.

Safety invariants (see docs/desktop_security.md):
- No typed text is ever stored raw. ``redact_computer_action`` strips
  the ``text`` / ``keys`` payload before it touches this store.
- Step results are validated against an allow-list; any forbidden
  field (raw ocr, window title, base64, png bytes, secrets) is
  rejected so logs/state can never leak desktop content.
- Direct mode is opt-in and TTL-bounded; ``is_direct_mode_active``
  is the single gate the executor consults before running a task.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings
from runner.file_lock import file_lock

_lock = threading.Lock()


ALLOWED_STATUSES = frozenset({
    "pending", "claimed", "completed", "failed", "expired", "cancelled",
})

TASK_ALLOWED_STATUSES = frozenset({
    "running", "done", "stopped", "blocked", "error",
})

# Fields a step *result* may carry. Everything else is forbidden so
# the store can never accumulate raw desktop content.
RESULT_ALLOWED_FIELDS = frozenset({
    "screenshot_id",
    "sha256",
    "width",
    "height",
    "obs_text_len",
    "obs_text_preview",
    "text_len",
    "keys_len",
    "action_type",
    "action_redacted",
    "result_ok",
    "effect",
    "path",
    "verified",
    "error",
    "node_id",
    "created_at",
})

RESULT_FORBIDDEN_FIELDS = frozenset({
    "png_bytes",
    "image_bytes",
    "base64",
    "data",
    "ocr",
    "ocr_text",
    "window_title",
    "app_name",
    "text",
    "keys",
    "thumbnail",
    "password",
    "secret",
    "token",
    "uploaded",
})

DEFAULT_ARM_TTL_MINUTES = 30


def computer_requests_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "state" / "desktop_computer_requests.json"


def computer_requests_lock_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "state" / "desktop_computer_requests.lock"


# ---- time helpers (copied from desktop_observe_requests for isolation) -----

def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truncate_text(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _new_task_id(now: datetime | None = None) -> str:
    ts = _utc_now(now).strftime("%Y%m%dT%H%M%SZ")
    return f"ctsk_{ts}_{uuid.uuid4().hex[:8]}"


def _new_step_id(now: datetime | None = None) -> str:
    ts = _utc_now(now).strftime("%Y%m%dT%H%M%SZ")
    return f"cstp_{ts}_{uuid.uuid4().hex[:8]}"


# ---- redaction --------------------------------------------------------------

def redact_computer_action(action: dict) -> dict:
    """Return a copy of an action with sensitive payloads redacted.

    Typed text and hotkey lists are the two payloads that could carry
    secrets. They are replaced with a redaction marker + length so the
    trajectory remains useful for auditing without leaking content.
    """
    if not isinstance(action, dict):
        return {"action": "unknown"}
    redacted = dict(action)
    act = redacted.get("action")
    if act == "type" and "text" in redacted:
        text = redacted.pop("text", "")
        if isinstance(text, str):
            redacted["text_len"] = len(text)
            redacted["text_redacted"] = "***"
    if act == "hotkey" and "keys" in redacted:
        keys = redacted.pop("keys", [])
        if isinstance(keys, list):
            redacted["keys_len"] = len(keys)
            redacted["keys_redacted"] = "***"
    return redacted


def _blocked_keywords(settings: Settings) -> tuple[str, ...]:
    kws = getattr(settings, "conveyor_computer_blocked_keywords", None)
    if isinstance(kws, (tuple, list)):
        return tuple(str(k).strip().lower() for k in kws if str(k).strip())
    return ()


def contains_blocked_keyword(settings: Settings, text: str) -> str | None:
    """Return the first blocked keyword found in ``text`` (lowercased), else None."""
    text = (text or "").lower()
    if not text:
        return None
    for kw in _blocked_keywords(settings):
        if kw and kw in text:
            return kw
    return None


def is_action_allowed(settings: Settings, action: dict) -> bool:
    """True when the action type is in the configured allow-list."""
    if not isinstance(action, dict):
        return False
    act = action.get("action")
    allowed = getattr(settings, "conveyor_computer_allowed_actions", None)
    if isinstance(allowed, (tuple, list)):
        return str(act) in set(str(a) for a in allowed)
    return str(act) in {
        "observe", "click", "type", "hotkey", "scroll", "wait",
        "done", "stop",
    }


def normalize_action(action: object) -> dict:
    """Coerce a planner output into a well-formed action dict."""
    if not isinstance(action, dict):
        return {"action": "stop", "reason": "planner_returned_non_object"}
    act = action.get("action")
    if act in ("done", "stop"):
        return {"action": act, "summary": action.get("summary"), "reason": action.get("reason")}
    norm: dict[str, Any] = {"action": str(act) if act is not None else "unknown"}
    for key in (
        "x", "y", "dx", "dy", "seconds", "text", "keys",
        "pid", "window_id", "element_index", "element_token",
        "delivery_mode", "scope", "button",
    ):
        if key in action:
            norm[key] = action[key]
    return norm


# ---- load / save ------------------------------------------------------------

def _load_unlocked(settings: Settings) -> dict[str, dict]:
    path = computer_requests_path(settings)
    if not path.exists():
        return {"tasks": {}, "arm": {}}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"tasks": {}, "arm": {}}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"tasks": {}, "arm": {}}
        data.setdefault("tasks", {})
        data.setdefault("arm", {})
        return data
    except Exception:
        return {"tasks": {}, "arm": {}}


def _save_unlocked(settings: Settings, store: dict[str, dict]) -> None:
    path = computer_requests_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except Exception:
        pass
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def load_computer_store(settings: Settings) -> dict[str, dict]:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            return _load_unlocked(settings)


def save_computer_store(settings: Settings, store: dict[str, dict]) -> None:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            _save_unlocked(settings, store)


# ---- direct-mode arming -----------------------------------------------------

def arm_direct_mode(settings: Settings, ttl_minutes: int | None = None) -> dict:
    """Arm direct (hands-free) mode for a TTL. Returns {ok, ...}."""
    if not settings.conveyor_computer_use_enabled:
        return {
            "ok": False,
            "error": "computer_use_disabled",
            "message": "CONVEYOR_COMPUTER_USE_ENABLED 未开启。",
        }
    if ttl_minutes is None or ttl_minutes <= 0:
        ttl_minutes = DEFAULT_ARM_TTL_MINUTES
    now = _utc_now()
    expires = now + timedelta(minutes=ttl_minutes)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            store["arm"] = {
                "active": True,
                "armed_at": _iso_z(now),
                "expires_at": _iso_z(expires),
                "ttl_minutes": int(ttl_minutes),
            }
            _save_unlocked(settings, store)
    return {
        "ok": True,
        "expires_at": _iso_z(expires),
        "ttl_minutes": int(ttl_minutes),
    }


def disarm_direct_mode(settings: Settings) -> dict:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            store["arm"] = {}
            _save_unlocked(settings, store)
    return {"ok": True}


def is_direct_mode_active(settings: Settings, now: datetime | None = None) -> bool:
    """True when hands-free mode is permitted.

    Two paths: (1) ``CONVEYOR_COMPUTER_ALWAYS_DIRECT=true`` (no arm
    needed), or (2) a non-expired arm record. Computer Use must also
    be enabled.
    """
    if not settings.conveyor_computer_use_enabled:
        return False
    if settings.conveyor_computer_always_direct:
        return True
    return _arm_active_unlocked(settings, now)


def direct_mode_source(settings: Settings, now: datetime | None = None) -> str | None:
    """Return 'always' | 'armed' | None describing why direct mode is on."""
    if not settings.conveyor_computer_use_enabled:
        return None
    if settings.conveyor_computer_always_direct:
        return "always"
    if _arm_active_unlocked(settings, now):
        return "armed"
    return None


def _arm_active_unlocked(settings: Settings, now: datetime | None = None) -> bool:
    store = _load_unlocked(settings)
    arm = store.get("arm") or {}
    if not isinstance(arm, dict) or not arm.get("active"):
        return False
    expires_at = _parse_iso(arm.get("expires_at"))
    if expires_at is None:
        return False
    return _utc_now(now) <= expires_at


def arm_remaining_seconds(settings: Settings, now: datetime | None = None) -> int:
    store = _load_unlocked(settings)
    arm = store.get("arm") or {}
    if not isinstance(arm, dict) or not arm.get("active"):
        return 0
    expires_at = _parse_iso(arm.get("expires_at"))
    if expires_at is None:
        return 0
    delta = (expires_at - _utc_now(now)).total_seconds()
    return max(0, int(delta))


# ---- task lifecycle ---------------------------------------------------------

def create_computer_task(
    settings: Settings,
    goal: str,
    *,
    direct_mode: bool,
    max_steps: int,
    max_seconds: int,
    operator_id: str = "",
    chat_id: str = "",
    channel: str = "",
) -> dict:
    if not settings.conveyor_computer_use_enabled:
        return {
            "ok": False,
            "error": "computer_use_disabled",
            "message": "CONVEYOR_COMPUTER_USE_ENABLED 未开启。",
        }
    goal = _truncate_text(goal, 2000)
    if not goal.strip():
        return {"ok": False, "error": "empty_goal", "message": "目标不能为空。"}
    now = _utc_now()
    task_id = _new_task_id(now)
    record = {
        "task_id": task_id,
        "goal": goal,
        "status": "running",
        "created_at": _iso_z(now),
        "updated_at": _iso_z(now),
        "expires_at": _iso_z(now + timedelta(seconds=max_seconds)),
        "direct_mode": bool(direct_mode),
        "operator_id": operator_id,
        "chat_id": chat_id,
        "channel": channel,
        "max_steps": int(max_steps),
        "max_seconds": int(max_seconds),
        "step_seq": 0,
        "steps": {},
        "trajectory": [],
        "summary": None,
        "blocked_reason": None,
    }
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            store.setdefault("tasks", {})[task_id] = record
            _save_unlocked(settings, store)
    return {"ok": True, "task_id": task_id, "task": dict(record)}


def get_computer_task(settings: Settings, task_id: str) -> dict | None:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            return dict(record) if isinstance(record, dict) else None


def get_active_task(settings: Settings) -> dict | None:
    """Return the single running task, if any (single-operator model)."""
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            for record in store.get("tasks", {}).values():
                if isinstance(record, dict) and record.get("status") == "running":
                    return dict(record)
    return None


def set_task_status(
    settings: Settings,
    task_id: str,
    status: str,
    *,
    summary: str | None = None,
    blocked_reason: str | None = None,
) -> dict:
    if status not in TASK_ALLOWED_STATUSES:
        return {"ok": False, "error": "invalid_status"}
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "task_not_found"}
            record["status"] = status
            record["updated_at"] = _iso_z(_utc_now())
            if summary is not None:
                record["summary"] = _truncate_text(summary, 2000)
            if blocked_reason is not None:
                record["blocked_reason"] = _truncate_text(blocked_reason, 500)
            _save_unlocked(settings, store)
    return {"ok": True, "task": dict(record)}


def cancel_computer_task(settings: Settings, task_id: str, reason: str = "operator_stop") -> dict:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "task_not_found"}
            if record.get("status") != "running":
                return {"ok": False, "error": "invalid_status", "status": record.get("status")}
            record["status"] = "stopped"
            record["blocked_reason"] = _truncate_text(reason, 500)
            record["updated_at"] = _iso_z(_utc_now())
            # Mark any pending/claimed steps as cancelled.
            for step in record.get("steps", {}).values():
                if isinstance(step, dict) and step.get("status") in ("pending", "claimed"):
                    step["status"] = "cancelled"
                    step["updated_at"] = _iso_z(_utc_now())
            _save_unlocked(settings, store)
    return {"ok": True, "task": dict(record)}


def append_trajectory(settings: Settings, task_id: str, entry: dict) -> dict:
    """Append a redacted trajectory entry: timestamp, action, result."""
    if not isinstance(entry, dict):
        return {"ok": False, "error": "bad_entry"}
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "task_not_found"}
            entry = dict(entry)
            entry.setdefault("ts", _iso_z(_utc_now()))
            record.setdefault("trajectory", []).append(entry)
            record["updated_at"] = _iso_z(_utc_now())
            _save_unlocked(settings, store)
    return {"ok": True}


def list_recent_computer_tasks(settings: Settings, *, limit: int = 5) -> list[dict]:
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            items: list[tuple[datetime, str, dict]] = []
            for task_id, record in store.get("tasks", {}).items():
                created = _parse_iso(record.get("created_at"))
                if created is None:
                    created = datetime.min.replace(tzinfo=timezone.utc)
                items.append((created, task_id, record))
            items.sort(key=lambda item: item[0], reverse=True)
            results = []
            for _, task_id, record in items[: max(0, limit)]:
                entry = dict(record)
                entry["task_id"] = task_id
                results.append(entry)
            return results


# ---- step lifecycle ---------------------------------------------------------

def create_computer_step(settings: Settings, task_id: str, action: dict) -> dict:
    """Create a pending step for ``action``. Returns {ok, step, ...}."""
    action = action if isinstance(action, dict) else {"action": "unknown"}
    now = _utc_now()
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "task_not_found"}
            if record.get("status") != "running":
                return {"ok": False, "error": "task_not_running", "status": record.get("status")}
            seq = int(record.get("step_seq", 0)) + 1
            record["step_seq"] = seq
            step_id = _new_step_id(now)
            step = {
                "step_id": step_id,
                "seq": seq,
                "task_id": task_id,
                # The pending step must carry the real action until the
                # Mac agent claims it; claim_computer_step redacts the
                # stored copy before returning to the caller.
                "action": dict(action),
                "action_redacted": redact_computer_action(action),
                "status": "pending",
                "created_at": _iso_z(now),
                "updated_at": _iso_z(now),
                "expires_at": _iso_z(now + timedelta(seconds=record.get("max_seconds", 600))),
                "claimed_at": None,
                "result": None,
                "error": None,
            }
            record.setdefault("steps", {})[step_id] = step
            _save_unlocked(settings, store)
    return {"ok": True, "step_id": step_id, "step": dict(step)}


def claim_computer_step(settings: Settings, step_id: str, node_id: str) -> dict:
    node_id = (node_id or "").strip()
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            step, task_id = _find_step_unlocked(store, step_id)
            if step is None:
                return {"ok": False, "error": "step_not_found"}
            if step.get("status") != "pending":
                return {"ok": False, "error": "invalid_status", "status": step.get("status")}
            # Capture the real action for delivery to the Mac, then
            # redact the stored copy so plaintext never lingers on the VPS.
            real_action = step.get("action")
            step["status"] = "claimed"
            step["updated_at"] = _iso_z(_utc_now())
            step["claimed_at"] = _iso_z(_utc_now())
            step["action"] = redact_computer_action(real_action)
            _save_unlocked(settings, store)
    # Return the *real* action so the agent can actually execute it.
    returned = dict(step)
    returned["action"] = real_action
    return {"ok": True, "step": returned, "task_id": task_id}


def complete_computer_step(settings: Settings, step_id: str, node_id: str, result: dict) -> dict:
    validated = validate_computer_result(result)
    if validated is None:
        return {"ok": False, "error": "invalid_result", "message": "结果含不允许的字段。"}
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            step, task_id = _find_step_unlocked(store, step_id)
            if step is None:
                return {"ok": False, "error": "step_not_found"}
            if step.get("status") != "claimed":
                return {"ok": False, "error": "invalid_status", "status": step.get("status")}
            step["status"] = "completed"
            step["updated_at"] = _iso_z(_utc_now())
            step["result"] = validated
            step["error"] = None
            _save_unlocked(settings, store)
    return {"ok": True, "step": dict(step), "task_id": task_id}


def fail_computer_step(
    settings: Settings,
    step_id: str,
    node_id: str,
    error: str,
    message: str | None = None,
) -> dict:
    error = _truncate_text(error or "step_failed", 128)
    safe_message = _truncate_text(message or "", 500) if message else None
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            step, task_id = _find_step_unlocked(store, step_id)
            if step is None:
                return {"ok": False, "error": "step_not_found"}
            if step.get("status") != "claimed":
                return {"ok": False, "error": "invalid_status", "status": step.get("status")}
            step["status"] = "failed"
            step["updated_at"] = _iso_z(_utc_now())
            step["error"] = error
            if safe_message:
                step["error_message"] = safe_message
            step["result"] = None
            _save_unlocked(settings, store)
    return {"ok": True, "step": dict(step), "task_id": task_id}


def validate_computer_result(result: object) -> dict | None:
    """Allow-list validation for step results (defence in depth)."""
    if not isinstance(result, dict):
        return None
    for key in result:
        if key in RESULT_FORBIDDEN_FIELDS:
            return None
        if key not in RESULT_ALLOWED_FIELDS:
            return None
    cleaned: dict[str, Any] = {}
    for field in RESULT_ALLOWED_FIELDS:
        value = result.get(field)
        if value is not None:
            cleaned[field] = value
    return cleaned


def _find_step_unlocked(store: dict[str, dict], step_id: str) -> tuple[dict | None, str | None]:
    for task_id, record in store.get("tasks", {}).items():
        if not isinstance(record, dict):
            continue
        step = record.get("steps", {}).get(step_id)
        if isinstance(step, dict):
            return step, task_id
    return None, None


def list_pending_computer_steps(settings: Settings, *, limit: int = 1) -> list[dict]:
    """Return pending steps (oldest first), each annotated with task_id."""
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            pending: list[tuple[datetime, str, dict, str]] = []
            for task_id, record in store.get("tasks", {}).items():
                if not isinstance(record, dict):
                    continue
                for step_id, step in record.get("steps", {}).items():
                    if not isinstance(step, dict):
                        continue
                    if step.get("status") != "pending":
                        continue
                    created = _parse_iso(step.get("created_at"))
                    if created is None:
                        created = datetime.min.replace(tzinfo=timezone.utc)
                    pending.append((created, step_id, step, task_id))
            pending.sort(key=lambda item: item[0])
            results = []
            for _, step_id, step, task_id in pending[: max(0, limit)]:
                entry = {
                    "step_id": step_id,
                    "task_id": task_id,
                    "seq": step.get("seq"),
                    "status": step.get("status"),
                    "created_at": step.get("created_at"),
                    "expires_at": step.get("expires_at"),
                    "action_redacted": step.get("action_redacted") or redact_computer_action(step.get("action") or {}),
                }
                results.append(entry)
            return results


def expire_old_computer(settings: Settings, now: datetime | None = None) -> int:
    """Expire stale arms and steps. Returns number of changed records."""
    changed = 0
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            # arm
            arm = store.get("arm") or {}
            if isinstance(arm, dict) and arm.get("active"):
                expires_at = _parse_iso(arm.get("expires_at"))
                if expires_at is not None and _utc_now(now) > expires_at:
                    store["arm"] = {}
                    changed += 1
            # steps
            for record in store.get("tasks", {}).values():
                if not isinstance(record, dict):
                    continue
                for step in record.get("steps", {}).values():
                    if not isinstance(step, dict):
                        continue
                    if step.get("status") in ("completed", "failed", "cancelled", "expired"):
                        continue
                    expires_at = _parse_iso(step.get("expires_at"))
                    if expires_at is not None and _utc_now(now) > expires_at:
                        step["status"] = "expired"
                        step["updated_at"] = _iso_z(_utc_now(now))
                        changed += 1
            if changed:
                _save_unlocked(settings, store)
    return changed


def clear_all_computer_state(settings: Settings) -> None:
    """Test helper: wipe tasks + arm."""
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            _save_unlocked(settings, {"tasks": {}, "arm": {}})
