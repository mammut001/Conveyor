"""desktop_upload_requests.py — P5.4 manual screenshot thumbnail upload request store."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from channel.types import InboundMessage
from config import Settings
from runner.file_lock import file_lock

_lock = threading.Lock()


ALLOWED_STATUSES = frozenset({
    "pending", "claimed", "completed", "failed", "expired", "cancelled",
})

RESULT_ALLOWED_FIELDS = frozenset({
    "upload_id",
    "thumbnail_path",
    "sha256",
    "bytes",
    "width",
    "height",
    "created_at",
    "source_screenshot_id",
    "node_id",
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
})


def upload_requests_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "state" / "desktop_upload_requests.json"


def upload_requests_lock_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "state" / "desktop_upload_requests.lock"


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


def _new_upload_id(now: datetime | None = None) -> str:
    ts = _utc_now(now).strftime("%Y%m%dT%H%M%SZ")
    return f"upl_{ts}_{uuid.uuid4().hex[:8]}"


def validate_upload_result(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    for key in result:
        if key in RESULT_FORBIDDEN_FIELDS:
            return None
        if key not in RESULT_ALLOWED_FIELDS:
            return None
    upload_id = result.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id.strip():
        return None
    sha = result.get("sha256")
    if not isinstance(sha, str) or not sha:
        return None
    thumbnail_path = result.get("thumbnail_path")
    if not isinstance(thumbnail_path, str) or not thumbnail_path.startswith("/"):
        return None
    cleaned: dict[str, Any] = {
        "upload_id": upload_id.strip(),
        "thumbnail_path": thumbnail_path,
        "sha256": sha,
    }
    for field in (
        "bytes", "width", "height", "created_at", "source_screenshot_id", "node_id"
    ):
        value = result.get(field)
        if value is not None:
            cleaned[field] = value
    return cleaned


def _load_unlocked(settings: Settings) -> dict[str, dict]:
    path = upload_requests_path(settings)
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


def _save_unlocked(settings: Settings, requests: dict[str, dict]) -> None:
    path = upload_requests_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except Exception:
        pass
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(requests, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def load_upload_requests(settings: Settings) -> dict[str, dict]:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            return _load_unlocked(settings)


def save_upload_requests(settings: Settings, requests: dict[str, dict]) -> None:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            _save_unlocked(settings, requests)


def _expire_old_unlocked(store: dict[str, dict], now: datetime | None = None) -> tuple[int, bool]:
    current = _utc_now(now)
    expired_count = 0
    changed = False
    for upload_id, record in list(store.items()):
        if not isinstance(record, dict):
            store.pop(upload_id, None)
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
    return expired_count, changed


def _count_pending_unlocked(store: dict[str, dict], node_id: str) -> int:
    count = 0
    for record in store.values():
        if record.get("node_id") == node_id and record.get("status") in ("pending", "claimed"):
            count += 1
    return count


def _list_pending_unlocked(store: dict[str, dict], node_id: str, limit: int = 1) -> list[dict]:
    results: list[dict] = []
    sorted_records = sorted(
        store.values(),
        key=lambda r: r.get("created_at") or ""
    )
    for record in sorted_records:
        if record.get("node_id") != node_id:
            continue
        if record.get("status") != "pending":
            continue
        results.append({
            "upload_id": record.get("upload_id"),
            "observe_request_id": record.get("observe_request_id"),
            "screenshot_id": record.get("screenshot_id"),
            "node_id": record.get("node_id"),
            "status": record.get("status"),
            "kind": record.get("kind"),
            "max_width": record.get("max_width"),
            "max_height": record.get("max_height"),
            "max_bytes": record.get("max_bytes"),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
        })
        if len(results) >= limit:
            break
    return results


def list_pending_upload_requests(
    settings: Settings,
    node_id: str,
    *,
    limit: int = 1,
) -> list[dict]:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            _, changed = _expire_old_unlocked(store)
            if changed:
                _save_unlocked(settings, store)
            return _list_pending_unlocked(store, node_id, limit)


def create_upload_request(
    settings: Settings,
    observe_record: dict,
    msg: InboundMessage,
    kind: str = "thumbnail",
) -> dict:
    if not settings.conveyor_desktop_upload_enabled:
        return {
            "ok": False,
            "error": "upload_disabled",
            "message": "Screenshot upload is disabled. Set CONVEYOR_DESKTOP_UPLOAD_ENABLED=true to allow manual thumbnail uploads.",
        }
    node_id = observe_record.get("node_id") or "macbook-payton"
    screenshot_id = observe_record.get("result", {}).get("screenshot_id")
    if not screenshot_id:
        return {
            "ok": False,
            "error": "missing_screenshot_id",
            "message": "Source observe request has no screenshot ID.",
        }
    observe_id = observe_record.get("request_id")

    # Dedup: if a thumbnail upload for this observe already exists, return it (idempotent)
    # Prevents duplicate uploads from repeated /observe_upload or auto chains.
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            _expire_old_unlocked(store)
            for rec in store.values():
                if (isinstance(rec, dict) and
                        rec.get("observe_request_id") == observe_id and
                        rec.get("kind") == kind and
                        rec.get("screenshot_id") == screenshot_id):
                    return {"ok": True, "request": dict(rec), "existing": True}
            # now proceed to create new
            if _count_pending_unlocked(store, node_id) >= 3:
                return {
                    "ok": False,
                    "error": "too_many_pending_uploads",
                    "message": "Too many pending upload requests. Wait for one to complete or expire.",
                }
            now = _utc_now()
            ttl = settings.conveyor_desktop_upload_ttl_seconds
            upload_id = _new_upload_id(now)

            record = {
                "upload_id": upload_id,
                "observe_request_id": observe_id,
                "screenshot_id": screenshot_id,
                "node_id": node_id,
                "status": "pending",
                "kind": kind,
                "created_at": _iso_z(now),
                "updated_at": _iso_z(now),
                "created_by_channel": msg.channel,
                "created_by_chat_id": msg.chat_id,
                "created_by_operator_id": msg.operator_id,
                "expires_at": _iso_z(now + timedelta(seconds=ttl)),
                "max_width": settings.conveyor_desktop_upload_max_width,
                "max_height": settings.conveyor_desktop_upload_max_height,
                "max_bytes": settings.conveyor_desktop_upload_max_bytes,
                "result": None,
                "error": None,
            }
            store[upload_id] = record
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def claim_upload_request(settings: Settings, upload_id: str, node_id: str) -> dict:
    node_id = (node_id or "").strip()
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            _expire_old_unlocked(store)
            record = store.get(upload_id)
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
                _save_unlocked(settings, store)
                return {"ok": False, "error": "request_expired"}
            now = _iso_z(_utc_now())
            record["status"] = "claimed"
            record["updated_at"] = now
            record["claimed_at"] = now
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def complete_upload_request(
    settings: Settings,
    upload_id: str,
    node_id: str,
    result: dict,
) -> dict:
    validated = validate_upload_result(result)
    if validated is None:
        return {"ok": False, "error": "invalid_result", "message": "Result must be metadata only."}

    node_id = (node_id or "").strip()
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
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
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def fail_upload_request(
    settings: Settings,
    upload_id: str,
    node_id: str,
    error: str,
    message: str | None = None,
) -> dict:
    node_id = (node_id or "").strip()
    error = _truncate_text(error or "upload_failed", 128)
    safe_message = _truncate_text(message or "", 500) if message else None
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
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
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def cancel_upload_request(settings: Settings, upload_id: str) -> dict:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "request_not_found"}
            status = record.get("status")
            if status not in ("pending", "claimed"):
                return {"ok": False, "error": "invalid_status", "status": status}
            record["status"] = "cancelled"
            record["updated_at"] = _iso_z(_utc_now())
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def list_recent_upload_requests(settings: Settings, limit: int = 5) -> list[dict]:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            _expire_old_unlocked(store)
            items = []
            for upload_id, record in store.items():
                created = _parse_iso(record.get("created_at"))
                if created is None:
                    created = datetime.min.replace(tzinfo=timezone.utc)
                items.append((created, upload_id, record))
            items.sort(key=lambda item: item[0], reverse=True)
            results = []
            for _, _, record in items[:limit]:
                results.append(dict(record))
            return results


def get_upload_request(settings: Settings, upload_id: str) -> dict | None:
    store = load_upload_requests(settings)
    return store.get(upload_id)


def mark_upload_delivered(
    settings: Settings,
    upload_id: str,
    *,
    channel: str | None = None,
    chat_id: str | None = None,
    delivered_at: datetime | None = None,
) -> dict:
    now = _utc_now(delivered_at)
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "request_not_found"}
            if record.get("status") != "completed":
                return {"ok": False, "error": "invalid_status", "status": record.get("status")}
            record["delivered"] = True
            record["delivered_at"] = _iso_z(now)
            if channel is not None:
                record["delivered_channel"] = channel
            if chat_id is not None:
                record["delivered_chat_id"] = chat_id
            record["delivery_error"] = None
            record["delivery_error_message"] = None
            record["delivery_failed_at"] = None
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def mark_upload_delivery_failed(
    settings: Settings,
    upload_id: str,
    error: str,
    *,
    message: str | None = None,
    failed_at: datetime | None = None,
) -> dict:
    now = _utc_now(failed_at)
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "request_not_found"}
            if record.get("status") != "completed":
                return {"ok": False, "error": "invalid_status", "status": record.get("status")}
            record["delivered"] = False
            record["delivery_failed"] = True
            record["delivery_failed_at"] = _iso_z(now)
            record["delivery_error"] = _truncate_text(error, 128)
            if message:
                record["delivery_error_message"] = _truncate_text(message, 500)
            else:
                record["delivery_error_message"] = None
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def reset_upload_delivery(settings: Settings, upload_id: str) -> dict:
    with _lock:
        with file_lock(upload_requests_lock_path(settings)):
            store = _load_unlocked(settings)
            record = store.get(upload_id)
            if not isinstance(record, dict):
                return {"ok": False, "error": "request_not_found"}
            if record.get("status") != "completed":
                return {"ok": False, "error": "invalid_status", "status": record.get("status")}
            record["delivered"] = False
            record["delivered_at"] = None
            record["delivered_channel"] = None
            record["delivered_chat_id"] = None
            record["delivery_failed"] = False
            record["delivery_error"] = None
            record["delivery_error_message"] = None
            record["delivery_failed_at"] = None
            _save_unlocked(settings, store)
            return {"ok": True, "request": dict(record)}


def ensure_upload_request_for_observe(
    settings: Settings,
    observe_record: dict,
    *,
    created_by_channel: str,
    created_by_chat_id: str,
    created_by_operator_id: str | None = None,
) -> dict:
    """Idempotent helper: create (or return existing) thumbnail upload for a completed observe.

    - Returns existing if one for the observe_request_id + thumbnail kind already exists.
    - Only creates for completed observe with screenshot_id.
    - Respects CONVEYOR_DESKTOP_UPLOAD_ENABLED.
    - File-lock safe (delegates to create which now dedups under lock).
    - Preserves original chat/channel from caller (used for delivery target).
    - Does not create duplicates on repeated calls.
    """
    if not settings.conveyor_desktop_upload_enabled:
        return {
            "ok": False,
            "error": "upload_disabled",
            "message": "Screenshot upload is disabled. Set CONVEYOR_DESKTOP_UPLOAD_ENABLED=true to allow thumbnail auto-delivery.",
        }

    if not isinstance(observe_record, dict):
        return {"ok": False, "error": "invalid_observe_record"}

    if observe_record.get("status") != "completed":
        return {"ok": False, "error": "observe_not_completed"}

    observe_id = observe_record.get("request_id")
    result = observe_record.get("result") or {}
    screenshot_id = result.get("screenshot_id") if isinstance(result, dict) else None
    if not observe_id or not screenshot_id:
        return {"ok": False, "error": "missing_screenshot_id"}

    # Build a proxy InboundMessage-like for create (channel/chat preserved for delivery)
    class _ProxyMsg:
        channel = created_by_channel
        chat_id = created_by_chat_id
        operator_id = created_by_operator_id

    proxy = _ProxyMsg()
    return create_upload_request(settings, observe_record, proxy, kind="thumbnail")
