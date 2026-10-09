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
import hashlib
import os
import re
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
    # LocalCuaTransport metadata (short strings only; no window titles).
    "active_app",
    "click_method",
    # AX hints from observe so planner can emit AX clicks (not bare x/y).
    "pid",
    "window_id",
    "ax_app",
    "element_hints",
    # Short window list so the planner can focus an app it cannot see as pixels.
    "windows",
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


def _idempotency_digest(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---- AX Validation & App Allowlist/Blocklist --------------------------------

def validate_ax_fields(action: dict) -> tuple[bool, str | None]:
    for key in ("pid", "window_id", "element_index"):
        v = action.get(key)
        if v is not None:
            try:
                val = int(v)
                if key == "pid" and val <= 0:
                    return False, f"pid must be positive integer, got {v}"
                if key in ("window_id", "element_index") and val < 0:
                    return False, f"{key} must be non-negative integer, got {v}"
            except (ValueError, TypeError):
                return False, f"{key} must be an integer, got {v}"
    return True, None


# Read-only / control actions: allowlist does not apply (blocklist still does).
# Planner first step is almost always bare ``observe`` while Codex/Terminal is
# frontmost; requiring allowlist match there blocks the whole task before any
# Calculator AX click can run.
_ALLOWLIST_EXEMPT_ACTIONS = frozenset({"observe", "wait", "done", "stop"})

# Financial, payment, transfer, system administration, and destructive contexts
# remain strictly blocked even if an operator customizes the optional keyword list.
_HARD_BLOCKED_KEYWORDS = (
    "bank", "payment", "crypto", "keychain", "system settings", "delete account",
    "支付", "转账", "付款", "交易密码", "收银台", "银行",
)

_LOGIN_PASSWORD_KEYWORDS = frozenset({
    "password", "passcode", "密码", "口令",
})


def action_enforces_app_allowlist(action: object) -> bool:
    """True when this action type must match CONVEYOR_COMPUTER_ALLOWED_APPS."""
    if not isinstance(action, dict):
        return True
    act = str(action.get("action") or "").strip().lower()
    return act not in _ALLOWLIST_EXEMPT_ACTIONS


def check_app_allowlist_blocklist(
    settings: Settings,
    app_name: str,
    *,
    enforce_allowlist: bool = True,
) -> tuple[bool, str | None]:
    """Validate app against blocklist always; allowlist only when requested.

    ``enforce_allowlist=False`` is used for read-only observe/wait so a
    Calculator-only allowlist does not reject the planner's initial
    desktop observe while Codex is frontmost. Mutating actions
    (click/type/hotkey/scroll) always pass ``enforce_allowlist=True``.
    """
    if not app_name:
        return True, None
    app_lower = app_name.strip().lower()

    # 1. Blocked apps check (always)
    blocked_apps = getattr(settings, "conveyor_computer_blocked_apps", ())
    for b in blocked_apps:
        if b.strip().lower() == app_lower:
            return False, f"blocked_app:{app_name}"

    # 2. Allowed apps check (mutating actions only)
    if not enforce_allowlist:
        return True, None
    allowed_apps = getattr(settings, "conveyor_computer_allowed_apps", ())
    if allowed_apps:
        matched = False
        for a in allowed_apps:
            if a.strip().lower() == app_lower:
                matched = True
                break
        if not matched:
            return False, f"app_not_in_allowlist:{app_name}"

    return True, None


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
    # AX element tokens are executable routing handles, not audit data. Keep
    # them only in the short-lived action delivered to the local agent.
    redacted.pop("element_token", None)
    # Keep short UI labels for trajectory completion checks; drop long free text.
    for lab_key in ("_target_label", "label"):
        if lab_key in redacted:
            lab = redacted.get(lab_key)
            if isinstance(lab, str) and lab.strip() and len(lab.strip()) <= 32:
                redacted[lab_key] = lab.strip()
            else:
                redacted.pop(lab_key, None)
    return redacted


def _blocked_keywords(settings: Settings) -> tuple[str, ...]:
    allow_login = getattr(settings, "conveyor_computer_allow_login_passwords", False)
    kws = getattr(settings, "conveyor_computer_blocked_keywords", None)
    if isinstance(kws, (tuple, list)):
        configured = [str(k).strip().lower() for k in kws if str(k).strip()]
    else:
        configured = []
    combined = dict.fromkeys((*_HARD_BLOCKED_KEYWORDS, *configured))
    if allow_login:
        for kw in _LOGIN_PASSWORD_KEYWORDS:
            combined.pop(kw, None)
    return tuple(combined)


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
        "_target_label", "label",
        "target_app", "ensure_browser",
        "_mock_active_app", "_mock_target_app",
        "_mock_element_hints", "_mock_pid", "_mock_window_id", "_mock_ax_app",
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
    """Arm direct (hands-free) mode for a TTL. Returns {ok, ...}.

    Requires both CONVEYOR_COMPUTER_USE_ENABLED and
    CONVEYOR_COMPUTER_DIRECT_ENABLED. USE alone only unlocks status /
    read-only readiness, not arming or action execution.
    """
    if not settings.conveyor_computer_use_enabled:
        return {
            "ok": False,
            "error": "computer_use_disabled",
            "message": "CONVEYOR_COMPUTER_USE_ENABLED 未开启。",
        }
    if not settings.conveyor_computer_direct_enabled:
        return {
            "ok": False,
            "error": "computer_direct_disabled",
            "message": "CONVEYOR_COMPUTER_DIRECT_ENABLED 未开启。",
        }
    if ttl_minutes is None or ttl_minutes <= 0:
        ttl_minutes = DEFAULT_ARM_TTL_MINUTES
    now = _utc_now()
    expires = now + timedelta(minutes=ttl_minutes)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            # Do not re-arm direct computer use while a human owns the GUI.
            from human_takeover import takeover_blocks_automation
            if takeover_blocks_automation(settings):
                return {"ok": False, "error": "human_takeover_active"}
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

    Requires both USE and DIRECT enabled. Then either:
    (1) ``CONVEYOR_COMPUTER_ALWAYS_DIRECT=true`` (no arm needed), or
    (2) a non-expired arm record.

    ALWAYS_DIRECT alone never bypasses a missing DIRECT_ENABLED flag.
    """
    if not settings.conveyor_computer_use_enabled:
        return False
    if not settings.conveyor_computer_direct_enabled:
        return False
    if settings.conveyor_computer_always_direct:
        return True
    return _arm_active_unlocked(settings, now)


def direct_mode_source(settings: Settings, now: datetime | None = None) -> str | None:
    """Return 'always' | 'armed' | None describing why direct mode is on."""
    if not settings.conveyor_computer_use_enabled:
        return None
    if not settings.conveyor_computer_direct_enabled:
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

HOST_SCOPE = "default"


def task_scope(task: object) -> str:
    """Desktop a task acts on: the host's, or ``agent:<id>`` for an agent's own.

    It is also the takeover-lease scope, so a human on one desktop pauses
    only the work aimed at that desktop.
    """
    scope = task.get("takeover_scope") if isinstance(task, dict) else None
    return scope if isinstance(scope, str) and scope else HOST_SCOPE


def x11_node_id(scope: str) -> str:
    """Node name of the in-process executor for an agent desktop."""
    return f"x11:{scope}"


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
    idempotency_key: str = "",
    retry_of: str | None = None,
    single_active: bool = False,
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
    idem_digest = _idempotency_digest(idempotency_key)
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
        "idempotency_key_sha256": idem_digest,
        "retry_of": retry_of,
        "root_task_id": task_id,
        "attempt": 1,
    }
    # The conversation decides the desktop: an agent with its own display
    # works there, everything else on the shared host desktop.
    import agents
    target = agents.computer_target_for_chat(settings, channel, chat_id)
    record["takeover_scope"] = target["scope"]
    if target.get("agent_id"):
        record["agent_id"] = target["agent_id"]
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            tasks = store.setdefault("tasks", {})
            if idem_digest:
                for existing_id, existing in tasks.items():
                    if (
                        isinstance(existing, dict)
                        and existing.get("idempotency_key_sha256") == idem_digest
                    ):
                        return {
                            "ok": True,
                            "task_id": existing_id,
                            "task": dict(existing),
                            "deduplicated": True,
                        }
            if single_active:
                for existing_id, existing in tasks.items():
                    # One task at a time per desktop, not per installation.
                    if (
                        isinstance(existing, dict)
                        and existing.get("status") == "running"
                        and task_scope(existing) == record["takeover_scope"]
                    ):
                        return {
                            "ok": False,
                            "error": "computer_task_active",
                            "active_task_id": existing_id,
                            "message": f"已有 Computer Use 任务 {existing_id} 正在运行。",
                        }
            if retry_of:
                parent = tasks.get(retry_of)
                if not isinstance(parent, dict):
                    return {"ok": False, "error": "retry_task_not_found"}
                record["root_task_id"] = parent.get("root_task_id") or retry_of
                try:
                    record["attempt"] = int(parent.get("attempt") or 1) + 1
                except (TypeError, ValueError):
                    record["attempt"] = 2
            tasks[task_id] = record
            _save_unlocked(settings, store)
    return {"ok": True, "task_id": task_id, "task": dict(record)}


def retry_computer_task(
    settings: Settings,
    task_id: str,
    *,
    idempotency_key: str = "",
    operator_id: str = "",
    chat_id: str = "",
    channel: str = "",
) -> dict:
    """Create a linked fresh attempt for a terminal task.

    Previous actions are never replayed. The new loop starts from a fresh
    observation, which is the only safe resume primitive for mutable UI state.
    """
    previous = get_computer_task(settings, task_id)
    if not isinstance(previous, dict):
        return {"ok": False, "error": "task_not_found", "message": f"未找到任务 {task_id}。"}
    status = str(previous.get("status") or "")
    if status == "running":
        return {"ok": False, "error": "task_still_running", "message": "任务仍在运行，请先停止。"}
    if status == "done":
        return {"ok": False, "error": "task_already_done", "message": "任务已完成，无需重试。"}
    return create_computer_task(
        settings,
        str(previous.get("goal") or ""),
        direct_mode=True,
        max_steps=int(previous.get("max_steps") or settings.conveyor_computer_max_steps),
        max_seconds=int(previous.get("max_seconds") or settings.conveyor_computer_max_seconds),
        operator_id=operator_id or str(previous.get("operator_id") or ""),
        chat_id=chat_id or str(previous.get("chat_id") or ""),
        channel=channel or str(previous.get("channel") or ""),
        idempotency_key=idempotency_key,
        retry_of=task_id,
        single_active=True,
    )


def get_computer_task(settings: Settings, task_id: str) -> dict | None:
    # A task may outlive the executor process. Reconcile its deadline before
    # returning it so loop polling and /computer_log never expose stale
    # ``running`` state indefinitely.
    expire_old_computer(settings)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            return dict(record) if isinstance(record, dict) else None


_SESSION_THREAD = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SCREENSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
COMPUTER_SESSION_MAX_AGE_SECONDS = 30 * 60


def computer_session_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "state" / "computer_session.json"


def safe_screenshot_id(value: object) -> str:
    """Screenshot file stem. Rejects paths and anything that is not a stem."""
    if not isinstance(value, str) or not _SCREENSHOT_ID.fullmatch(value):
        return ""
    return value


def save_computer_session(
    settings: Settings,
    *,
    thread_id: str,
    task_id: str,
    goal: str,
    status: str,
    screenshot_id: str = "",
) -> None:
    """Remember the Codex thread so the next short click can continue it."""
    if status == "blocked":
        clear_computer_session(settings)
        return
    if status not in {"done", "stopped", "error"}:
        return
    if not isinstance(thread_id, str) or not _SESSION_THREAD.fullmatch(thread_id):
        return
    payload = {
        "thread_id": thread_id,
        "task_id": str(task_id or "")[:80],
        "goal": _truncate_text(goal, 500),
        "status": status,
        "screenshot_id": safe_screenshot_id(screenshot_id),
        "updated_at": _iso_z(_utc_now()),
    }
    path = computer_session_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def clear_computer_session(settings: Settings) -> None:
    path = computer_session_path(settings)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def load_computer_session(settings: Settings, *, now: datetime | None = None) -> dict | None:
    """Return a recent desktop thread, or None when it is missing or stale."""
    path = computer_session_path(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    thread_id = raw.get("thread_id")
    if not isinstance(thread_id, str) or not _SESSION_THREAD.fullmatch(thread_id):
        return None
    if raw.get("status") not in {"done", "stopped", "error"}:
        return None
    updated = _parse_iso(raw.get("updated_at"))
    if updated is None:
        return None
    age = (_utc_now(now) - updated).total_seconds()
    if age < 0 or age > COMPUTER_SESSION_MAX_AGE_SECONDS:
        return None
    return dict(raw)


def get_active_task(settings: Settings) -> dict | None:
    """Return the single running task, if any (single-operator model)."""
    expire_old_computer(settings)
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
    only_if_running: bool = False,
) -> dict:
    if status not in TASK_ALLOWED_STATUSES:
        return {"ok": False, "error": "invalid_status"}
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get("tasks", {}).get(task_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "task_not_found"}
            if only_if_running and record.get("status") != "running":
                return {
                    "ok": False,
                    "error": "task_not_running",
                    "status": record.get("status"),
                    "task": dict(record),
                }
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
            ts = entry.setdefault("ts", _iso_z(_utc_now()))
            
            # Calculate step index before appending
            step_idx = len(record.setdefault("trajectory", []))
            
            record["trajectory"].append(entry)
            record["updated_at"] = _iso_z(_utc_now())
            _save_unlocked(settings, store)
            
            # Write JSONL log to disk (private dirs 0700, file 0600).
            try:
                import json
                from pathlib import Path
                computer_dir = Path(settings.codex_memory_root) / "computer"
                traj_dir = computer_dir / "trajectories"
                traj_dir.mkdir(parents=True, exist_ok=True)
                for d in (computer_dir, traj_dir):
                    try:
                        os.chmod(d, 0o700)
                    except OSError:
                        pass
                traj_file = traj_dir / f"{task_id}.jsonl"

                line_obj = {
                    "timestamp": ts,
                    "task_id": task_id,
                    "step": step_idx,
                    "screenshot_id": entry.get("screenshot_id"),
                    "screenshot_hash": entry.get("screenshot_hash"),
                    "action_type": entry.get("action_type"),
                    "redacted_args": entry.get("action_redacted"),
                    "result_status": "ok" if entry.get("result_ok") else "fail",
                    "error": entry.get("error"),
                    "duration_ms": entry.get("duration_ms", 0),
                }

                with open(traj_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line_obj, ensure_ascii=False) + "\n")
                try:
                    os.chmod(traj_file, 0o600)
                except OSError:
                    pass
            except Exception:
                pass
    return {"ok": True}


def list_recent_computer_tasks(settings: Settings, *, limit: int = 5) -> list[dict]:
    expire_old_computer(settings)
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
            # This is the final control-plane gate before an action is queued.
            # claim_computer_step repeats the check at the desktop-node edge.
            from human_takeover import takeover_blocks_automation
            if takeover_blocks_automation(settings, task_scope(record)):
                return {"ok": False, "error": "human_takeover_active"}
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


def cancel_pending_computer_steps(
    settings: Settings, reason: str = "human_takeover_active", *, scope: str = HOST_SCOPE,
) -> int:
    """Cancel queued actions when a human takes ownership of a desktop.

    Only steps aimed at that desktop (``scope``) are touched.

    A pending action was planned against the pre-takeover screen. Replaying it
    after the operator finishes could click/type into a different page, so it
    must be discarded and replaced by a fresh observe.
    """
    reason = _truncate_text(reason or "human_takeover_active", 128)
    changed = 0
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            now = _iso_z(_utc_now())
            for record in store.get("tasks", {}).values():
                if not isinstance(record, dict) or task_scope(record) != scope:
                    continue
                for step in (record.get("steps") or {}).values():
                    if not isinstance(step, dict) or step.get("status") != "pending":
                        continue
                    step["status"] = "cancelled"
                    step["updated_at"] = now
                    step["error"] = reason
                    changed += 1
            if changed:
                _save_unlocked(settings, store)
    return changed


def cancel_pending_computer_step(
    settings: Settings,
    task_id: str,
    step_id: str,
    reason: str = "human_takeover_active",
) -> bool:
    """Cancel one unclaimed stale action without interrupting an in-flight one."""
    reason = _truncate_text(reason or "human_takeover_active", 128)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            step, found_task_id = _find_step_unlocked(store, step_id)
            if step is None or found_task_id != task_id or step.get("status") != "pending":
                return False
            step["status"] = "cancelled"
            step["updated_at"] = _iso_z(_utc_now())
            step["error"] = reason
            _save_unlocked(settings, store)
            return True


def has_claimed_computer_steps(settings: Settings, *, scope: str = HOST_SCOPE) -> bool:
    """Return whether an action on that desktop is already in flight."""
    expire_old_computer(settings)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            return any(
                isinstance(step, dict) and step.get("status") == "claimed"
                for record in store.get("tasks", {}).values()
                if isinstance(record, dict) and task_scope(record) == scope
                for step in (record.get("steps") or {}).values()
            )


def claim_computer_step(settings: Settings, step_id: str, node_id: str) -> dict:
    node_id = (node_id or "").strip()
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            step, task_id = _find_step_unlocked(store, step_id)
            if step is None:
                return {"ok": False, "error": "step_not_found"}
            scope = task_scope(store.get("tasks", {}).get(task_id))
            # A step for an agent's desktop is executed in-process on that
            # display. The host's desktop node must never run it, and nothing
            # else may run the host's.
            is_x11_node = node_id.startswith("x11:")
            if (is_x11_node and scope == HOST_SCOPE) or (scope != HOST_SCOPE and node_id != x11_node_id(scope)):
                return {"ok": False, "error": "wrong_node"}
            # The loop's lease check alone is not enough: a pending step may
            # be claimed by the desktop node after takeover begins.
            from human_takeover import takeover_blocks_automation
            if takeover_blocks_automation(settings, scope):
                return {"ok": False, "error": "human_takeover_active"}
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


# Short string fields from the driver — truncate so state never grows.
_RESULT_SHORT_STRING_LIMITS = {
    "active_app": 128,
    "click_method": 64,
    "error": 256,
    "action_type": 64,
    "effect": 128,
    "node_id": 128,
    "screenshot_id": 256,
    "sha256": 128,
    "obs_text_preview": 200,
    "path": 512,
    "created_at": 64,
    "ax_app": 128,
}

_ELEMENT_HINT_MAX = 40
_ELEMENT_HINT_LABEL_MAX = 32
_ELEMENT_HINT_ROLE_MAX = 48
_ELEMENT_HINT_TOKEN_MAX = 64


_WINDOW_LIST_MAX = 12
_WINDOW_TITLE_MAX = 32
_WINDOW_APP_MAX = 64


def _clean_windows(value: object) -> list[dict[str, Any]] | None:
    """Keep a bounded window list: app, short title, geometry, ids."""
    if not isinstance(value, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in value[:_WINDOW_LIST_MAX]:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for key in ("z", "pid", "window_id", "x", "y", "w", "h"):
            if item.get(key) is None:
                continue
            try:
                row[key] = int(item[key])
            except (TypeError, ValueError):
                continue
        app = item.get("app")
        if isinstance(app, str) and app.strip():
            row["app"] = _truncate_text(app.strip(), _WINDOW_APP_MAX)
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            row["title"] = _truncate_text(title.strip(), _WINDOW_TITLE_MAX)
        if "pid" in row and "window_id" in row:
            cleaned.append(row)
    return cleaned or None


def _clean_element_hints(value: object) -> list[dict[str, Any]] | None:
    """Sanitize AX element hints: short labels/roles only, bounded list."""
    if not isinstance(value, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in value[:_ELEMENT_HINT_MAX]:
        if not isinstance(item, dict):
            continue
        hint: dict[str, Any] = {}
        idx = item.get("element_index")
        if idx is not None:
            try:
                hint["element_index"] = int(idx)
            except (TypeError, ValueError):
                continue
        role = item.get("role")
        if isinstance(role, str) and role.strip():
            hint["role"] = _truncate_text(role.strip(), _ELEMENT_HINT_ROLE_MAX)
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            # Drop long free text (could leak UI secrets); keep short labels.
            lab = label.strip()
            if len(lab) > _ELEMENT_HINT_LABEL_MAX:
                continue
            hint["label"] = lab
        if "element_index" in hint:
            cleaned.append(hint)
    return cleaned


def validate_computer_result(result: object) -> dict | None:
    """Allow-list validation for step results (defence in depth).

    Accepts only RESULT_ALLOWED_FIELDS. Short string metadata such as
    ``active_app`` and ``click_method`` (returned by LocalCuaTransport)
    is coerced and truncated so the store never holds long free text.
    AX observe hints (pid/window_id/element_hints) are accepted so the
    planner can emit AX clicks after the first observe.
    """
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
        if value is None:
            continue
        if field in ("pid", "window_id"):
            try:
                cleaned[field] = int(value)
            except (TypeError, ValueError):
                continue
            continue
        if field == "element_hints":
            hints = _clean_element_hints(value)
            if hints:
                cleaned[field] = hints
            continue
        if field == "windows":
            listed = _clean_windows(value)
            if listed:
                cleaned[field] = listed
            continue
        limit = _RESULT_SHORT_STRING_LIMITS.get(field)
        if limit is not None:
            if not isinstance(value, str):
                value = str(value)
            cleaned[field] = _truncate_text(value, limit)
        else:
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
                # This list is what the host's desktop node polls; steps for
                # an agent's own desktop are not its to run.
                if not isinstance(record, dict) or task_scope(record) != HOST_SCOPE:
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
    """Expire stale arms, tasks, and steps. Returns number of changes."""
    changed = 0
    current = _utc_now(now)
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            # arm
            arm = store.get("arm") or {}
            if isinstance(arm, dict) and arm.get("active"):
                expires_at = _parse_iso(arm.get("expires_at"))
                if expires_at is not None and current > expires_at:
                    store["arm"] = {}
                    changed += 1
            # A dead executor cannot run the loop's in-process max-seconds
            # guard, so expire the parent task as well as its steps.
            for record in store.get("tasks", {}).values():
                if not isinstance(record, dict):
                    continue
                task_expired = False
                if record.get("status") == "running":
                    task_expires_at = _parse_iso(record.get("expires_at"))
                    if task_expires_at is not None and current > task_expires_at:
                        record["status"] = "stopped"
                        record["blocked_reason"] = "max_seconds reached"
                        record["updated_at"] = _iso_z(current)
                        changed += 1
                        task_expired = True
                for step in record.get("steps", {}).values():
                    if not isinstance(step, dict):
                        continue
                    if step.get("status") in ("completed", "failed", "cancelled", "expired"):
                        continue
                    expires_at = _parse_iso(step.get("expires_at"))
                    if task_expired or (expires_at is not None and current > expires_at):
                        step["status"] = "expired"
                        step["updated_at"] = _iso_z(current)
                        changed += 1
            if changed:
                _save_unlocked(settings, store)
    return changed


def clear_all_computer_state(settings: Settings) -> None:
    """Test helper: wipe tasks + arm."""
    with _lock:
        with file_lock(computer_requests_lock_path(settings)):
            _save_unlocked(settings, {"tasks": {}, "arm": {}})
