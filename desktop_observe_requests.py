"""desktop_observe_requests.py — P5.3 remote read-only observe request store."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from channel.types import InboundMessage
from config import Settings

_lock = threading.Lock()

ALLOWED_STATUSES = frozenset({
    "pending", "claimed", "completed", "failed", "expired", "cancelled",
})

RESULT_ALLOWED_FIELDS = frozenset({
    "screenshot_id",
    "path",
    "metadata_path",
    "sha256",
    "width",
    "height",
    "display_id",
    "created_at",
    "bytes",
    "node_id",
    "helper_version",
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
    "thumbnail",
    "uploaded",
})


def observe_requests_path(settings: Settings) -> Any:
    from pathlib import Path
    return settings.codex_memory_root / "state" / "desktop_observe_requests.json"


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


def load_observe_requests(settings: Settings) -> dict[str, dict]:
    path = observe_requests_path(settings)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return {
            k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
    except Exception:
        return {}


def save_observe_requests(settings: Settings, requests: dict[str, dict]) -> None:
    path = observe_requests_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    payload = json.dumps(requests, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def _new_request_id(now: datetime | None = None) -> str:
    ts = _utc_now(now).strftime("%Y%m%dT%H%M%SZ")
    return f"obs_{ts}_{uuid.uuid4().hex[:8]}"


def validate_observe_result(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    for key in result:
        if key in RESULT_FORBIDDEN_FIELDS:
            return None
        if key not in RESULT_ALLOWED_FIELDS:
            return None
    screenshot_id = result.get("screenshot_id")
    if not isinstance(screenshot_id, str) or not screenshot_id.strip():
        return None
    sha = result.get("sha256")
    if not isinstance(sha, str) or not sha:
        return None
    path = result.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    metadata_path = result.get("metadata_path")
    if metadata_path is not None and (
        not isinstance(metadata_path, str) or not metadata_path.startswith("/")
    ):
        return None
    cleaned: dict[str, Any] = {
        "screenshot_id": screenshot_id.strip(),
        "path": path,
        "sha256": sha,
    }
    for field in (
        "metadata_path", "width", "height", "display_id",
        "created_at", "bytes", "node_id", "helper_version",
    ):
        value = result.get(field)
        if value is not None:
            cleaned[field] = value
    return cleaned


def expire_old_observe_requests(settings: Settings, now: datetime | None = None) -> int:
    current = _utc_now(now)
    expired_count = 0
    with _lock:
        store = load_observe_requests(settings)
        changed = False
        for request_id, record in list(store.items()):
            if not isinstance(record, dict):
                store.pop(request_id, None)
                changed = True
                continue
            status = record.get("status")
            if status in ("completed", "failed", "cancelled", "expired"):
                continue
            expires_at = _parse_iso(record.get("expires_at"))
            if expires_at is not None and current > expires_at:
                record["status"] = "expired"
                record["updated_at"] = _iso_z(current)
                expired_count += 1
                changed = True
        if changed:
            save_observe_requests(settings, store)
    return expired_count


def count_pending_requests(settings: Settings, node_id: str) -> int:
    expire_old_observe_requests(settings)
    node_id = (node_id or "").strip()
    store = load_observe_requests(settings)
    count = 0
    for record in store.values():
        if record.get("node_id") == node_id and record.get("status") == "pending":
            count += 1
    return count


def get_observe_request(settings: Settings, request_id: str) -> dict | None:
    expire_old_observe_requests(settings)
    store = load_observe_requests(settings)
    record = store.get(request_id)
    if not isinstance(record, dict):
        return None
    return dict(record)


def list_pending_observe_requests(
    settings: Settings,
    node_id: str,
    *,
    limit: int = 1,
) -> list[dict]:
    expire_old_observe_requests(settings)
    node_id = (node_id or "").strip()
    store = load_observe_requests(settings)
    pending: list[tuple[datetime, str, dict]] = []
    for request_id, record in store.items():
        if record.get("node_id") != node_id:
            continue
        if record.get("status") != "pending":
            continue
        created = _parse_iso(record.get("created_at"))
        if created is None:
            created = datetime.min.replace(tzinfo=timezone.utc)
        pending.append((created, request_id, record))
    pending.sort(key=lambda item: item[0])
    results: list[dict] = []
    for _, request_id, record in pending[: max(0, limit)]:
        results.append({
            "request_id": request_id,
            "node_id": record.get("node_id"),
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
            "user_request": record.get("user_request"),
        })
    return results


def list_recent_observe_requests(settings: Settings, *, limit: int = 5) -> list[dict]:
    expire_old_observe_requests(settings)
    store = load_observe_requests(settings)
    items: list[tuple[datetime, str, dict]] = []
    for request_id, record in store.items():
        created = _parse_iso(record.get("created_at"))
        if created is None:
            created = datetime.min.replace(tzinfo=timezone.utc)
        items.append((created, request_id, record))
    items.sort(key=lambda item: item[0], reverse=True)
    results: list[dict] = []
    for _, request_id, record in items[: max(0, limit)]:
        entry = dict(record)
        entry["request_id"] = request_id
        results.append(entry)
    return results


def _observe_prerequisites(settings: Settings) -> str | None:
    if not settings.conveyor_desktop_node_enabled:
        return "desktop_node_disabled"
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    from nodes.state import is_desktop_online
    if not is_desktop_online(settings, node_id):
        return "desktop_agent_offline"
    from desktop_screenshot import helper_configuration_error
    helper_error = helper_configuration_error(settings)
    if helper_error:
        return helper_error
    max_pending = settings.conveyor_desktop_observe_max_pending
    if count_pending_requests(settings, node_id) >= max_pending:
        return "too_many_pending_requests"
    return None


def create_observe_request(
    settings: Settings,
    msg: InboundMessage,
    user_request: str,
) -> dict:
    """Create a pending observe request. Returns {ok, ...} or {ok: False, error}."""
    prereq = _observe_prerequisites(settings)
    if prereq:
        messages = {
            "desktop_node_disabled": "Desktop node is not enabled.",
            "desktop_agent_offline": "Desktop agent is offline. Start `python desktop_agent.py --poll-observe` on the Mac.",
            "screenshot_helper_not_configured": "Screenshot helper is not configured.",
            "screenshot_helper_path_not_absolute": "CONVEYOR_DESKTOP_SCREENSHOT_HELPER must be an absolute path.",
            "too_many_pending_requests": "Too many pending observe requests. Wait for one to complete or expire.",
        }
        return {
            "ok": False,
            "error": prereq,
            "message": messages.get(prereq, "Remote observe is unavailable."),
        }

    now = _utc_now()
    ttl = settings.conveyor_desktop_observe_request_ttl_seconds
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    request_id = _new_request_id(now)

    record = {
        "request_id": request_id,
        "node_id": node_id,
        "status": "pending",
        "created_at": _iso_z(now),
        "updated_at": _iso_z(now),
        "created_by_channel": msg.channel,
        "created_by_chat_id": msg.chat_id,
        "created_by_operator_id": msg.operator_id,
        "user_request": _truncate_text(user_request),
        "expires_at": _iso_z(now + timedelta(seconds=ttl)),
        "result": None,
        "error": None,
    }

    with _lock:
        store = load_observe_requests(settings)
        store[request_id] = record
        save_observe_requests(settings, store)

    return {"ok": True, "request": dict(record)}


def claim_observe_request(settings: Settings, request_id: str, node_id: str) -> dict:
    expire_old_observe_requests(settings)
    node_id = (node_id or "").strip()
    with _lock:
        store = load_observe_requests(settings)
        record = store.get(request_id)
        if not isinstance(record, dict):
            return {"ok": False, "error": "request_not_found"}
        if record.get("node_id") != node_id:
            return {"ok": False, "error": "node_id_mismatch"}
        status = record.get("status")
        if status == "expired":
            return {"ok": False, "error": "request_expired"}
        if status == "cancelled":
            return {"ok": False, "error": "request_cancelled"}
        if status != "pending":
            return {"ok": False, "error": "invalid_status", "status": status}
        expires_at = _parse_iso(record.get("expires_at"))
        if expires_at is not None and _utc_now() > expires_at:
            record["status"] = "expired"
            record["updated_at"] = _iso_z(_utc_now())
            save_observe_requests(settings, store)
            return {"ok": False, "error": "request_expired"}
        now = _iso_z(_utc_now())
        record["status"] = "claimed"
        record["updated_at"] = now
        record["claimed_at"] = now
        save_observe_requests(settings, store)
        return {"ok": True, "request": dict(record)}


def complete_observe_request(
    settings: Settings,
    request_id: str,
    node_id: str,
    result: dict,
) -> dict:
    validated = validate_observe_result(result)
    if validated is None:
        return {"ok": False, "error": "invalid_result", "message": "Result must be metadata only."}

    node_id = (node_id or "").strip()
    with _lock:
        store = load_observe_requests(settings)
        record = store.get(request_id)
        if not isinstance(record, dict):
            return {"ok": False, "error": "request_not_found"}
        if record.get("node_id") != node_id:
            return {"ok": False, "error": "node_id_mismatch"}
        if record.get("status") != "claimed":
            return {"ok": False, "error": "invalid_status", "status": record.get("status")}
        record["status"] = "completed"
        record["updated_at"] = _iso_z(_utc_now())
        record["result"] = validated
        record["error"] = None
        save_observe_requests(settings, store)
        return {"ok": True, "request": dict(record)}


def fail_observe_request(
    settings: Settings,
    request_id: str,
    node_id: str,
    error: str,
    message: str | None = None,
) -> dict:
    node_id = (node_id or "").strip()
    error = _truncate_text(error or "observe_failed", 128)
    safe_message = _truncate_text(message or "", 500) if message else None
    with _lock:
        store = load_observe_requests(settings)
        record = store.get(request_id)
        if not isinstance(record, dict):
            return {"ok": False, "error": "request_not_found"}
        if record.get("node_id") != node_id:
            return {"ok": False, "error": "node_id_mismatch"}
        if record.get("status") != "claimed":
            return {"ok": False, "error": "invalid_status", "status": record.get("status")}
        record["status"] = "failed"
        record["updated_at"] = _iso_z(_utc_now())
        record["error"] = error
        if safe_message:
            record["error_message"] = safe_message
        record["result"] = None
        save_observe_requests(settings, store)
        return {"ok": True, "request": dict(record)}


def cancel_observe_request(settings: Settings, request_id: str) -> dict:
    with _lock:
        store = load_observe_requests(settings)
        record = store.get(request_id)
        if not isinstance(record, dict):
            return {"ok": False, "error": "request_not_found"}
        status = record.get("status")
        if status not in ("pending", "claimed"):
            return {"ok": False, "error": "invalid_status", "status": status}
        record["status"] = "cancelled"
        record["updated_at"] = _iso_z(_utc_now())
        save_observe_requests(settings, store)
        return {"ok": True, "request": dict(record)}