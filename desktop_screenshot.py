"""desktop_screenshot.py — read-only local screenshot observe (P5.2)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import Settings

logger = logging.getLogger("conveyor.desktop_screenshot")

SCREENSHOT_DIR_MODE = 0o700

_METADATA_FIELDS = (
    "screenshot_id",
    "path",
    "metadata_path",
    "sha256",
    "width",
    "height",
    "display_id",
    "created_at",
    "node_id",
    "helper_version",
    "bytes",
)


def resolve_screenshot_dir(settings: Settings) -> Path:
    configured = (settings.conveyor_desktop_screenshot_dir or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (settings.codex_memory_root / "desktop" / "screenshots").resolve()


def resolve_helper_path(settings: Settings) -> Path | None:
    """Return absolute helper path, or None if unset."""
    raw = (settings.conveyor_desktop_screenshot_helper or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    return path.resolve()


def helper_configuration_error(settings: Settings) -> str | None:
    """Return a safe error code when helper path is invalid, else None."""
    raw = (settings.conveyor_desktop_screenshot_helper or "").strip()
    if not raw:
        return "screenshot_helper_not_configured"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return "screenshot_helper_path_not_absolute"
    return None


def ensure_screenshot_dir(settings: Settings) -> Path:
    screenshot_dir = resolve_screenshot_dir(settings)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    try:
        screenshot_dir.chmod(SCREENSHOT_DIR_MODE)
    except OSError:
        logger.debug("could not chmod screenshot dir to 0o700: %s", screenshot_dir)
    return screenshot_dir


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_created_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_metadata_record(data: dict, metadata_path: Path) -> dict | None:
    if not isinstance(data, dict):
        return None
    screenshot_id = data.get("screenshot_id")
    if not isinstance(screenshot_id, str) or not screenshot_id.strip():
        screenshot_id = metadata_path.stem
    record = {
        "screenshot_id": screenshot_id,
        "path": data.get("path") if isinstance(data.get("path"), str) else None,
        "metadata_path": str(metadata_path.resolve()),
        "sha256": data.get("sha256") if isinstance(data.get("sha256"), str) else None,
        "width": data.get("width"),
        "height": data.get("height"),
        "display_id": data.get("display_id"),
        "created_at": data.get("created_at") if isinstance(data.get("created_at"), str) else None,
        "node_id": data.get("node_id") if isinstance(data.get("node_id"), str) else None,
        "helper_version": data.get("helper_version") if isinstance(data.get("helper_version"), str) else None,
        "bytes": data.get("bytes"),
    }
    if record["bytes"] is None and isinstance(record["path"], str):
        png_path = Path(record["path"])
        if png_path.is_file():
            record["bytes"] = png_path.stat().st_size
    return record


def list_screenshot_metadata(settings: Settings, *, limit: int = 5) -> list[dict]:
    """List safe screenshot metadata JSON records, newest first."""
    screenshot_dir = resolve_screenshot_dir(settings)
    if not screenshot_dir.is_dir():
        return []

    candidates: list[tuple[datetime, float, dict]] = []
    for metadata_path in screenshot_dir.glob("*.json"):
        if metadata_path.name.endswith(".tmp"):
            continue
        if metadata_path.is_symlink():
            continue
        try:
            resolved_metadata_path = metadata_path.resolve()
            resolved_dir = screenshot_dir.resolve()
            try:
                is_relative = resolved_metadata_path.is_relative_to(resolved_dir)
            except AttributeError:
                try:
                    resolved_metadata_path.relative_to(resolved_dir)
                    is_relative = True
                except ValueError:
                    is_relative = False
            if not is_relative:
                continue
        except Exception:
            continue

        try:
            raw = metadata_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        record = _normalize_metadata_record(data, metadata_path)
        if record is None:
            continue
        created = _parse_created_at(record.get("created_at"))
        if created is not None:
            sort_key = created
        else:
            try:
                sort_key = datetime.fromtimestamp(metadata_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                sort_key = datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((sort_key, metadata_path.stat().st_mtime, record))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in candidates[: max(0, limit)]]


def latest_screenshot_metadata(settings: Settings) -> dict | None:
    records = list_screenshot_metadata(settings, limit=1)
    return records[0] if records else None


def validate_helper_payload(
    payload: dict,
    *,
    expected_output_path: Path,
    max_bytes: int,
) -> dict:
    """Validate helper JSON and the saved PNG. Returns safe dict."""
    if not payload.get("ok"):
        return {
            "ok": False,
            "error": payload.get("error", "helper_failed"),
            "message": payload.get("message", "Screenshot helper failed."),
        }

    reported_path = payload.get("path")
    if not isinstance(reported_path, str) or not reported_path:
        return {
            "ok": False,
            "error": "invalid_helper_response",
            "message": "Helper response missing path.",
        }

    if reported_path != str(expected_output_path):
        return {
            "ok": False,
            "error": "path_mismatch",
            "message": "Helper path does not match requested output path.",
        }

    if not reported_path.startswith("/"):
        return {
            "ok": False,
            "error": "relative_output_path",
            "message": "Helper returned a relative output path.",
        }

    output_path = Path(reported_path)
    if not output_path.is_file():
        return {
            "ok": False,
            "error": "output_missing",
            "message": "Screenshot file was not created.",
        }

    size = output_path.stat().st_size
    if size > max_bytes:
        return {
            "ok": False,
            "error": "screenshot_too_large",
            "message": f"Screenshot exceeds max size ({max_bytes} bytes).",
        }

    reported_sha = payload.get("sha256")
    if not isinstance(reported_sha, str) or not reported_sha:
        return {
            "ok": False,
            "error": "invalid_helper_response",
            "message": "Helper response missing sha256.",
        }

    actual_sha = _sha256_file(output_path)
    if actual_sha != reported_sha:
        return {
            "ok": False,
            "error": "sha256_mismatch",
            "message": "Screenshot sha256 does not match helper response.",
        }

    return {
        "ok": True,
        "path": reported_path,
        "sha256": actual_sha,
        "width": payload.get("width"),
        "height": payload.get("height"),
        "display_id": payload.get("display_id"),
        "created_at": payload.get("created_at"),
        "helper_version": payload.get("helper_version"),
        "bytes": size,
    }


def _write_metadata_atomic(metadata_path: Path, metadata: dict) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = metadata_path.with_name(metadata_path.name + ".tmp")
    payload = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, metadata_path)


def write_screenshot_metadata(
    settings: Settings,
    *,
    screenshot_id: str,
    capture: dict,
    node_id: str,
) -> Path:
    screenshot_dir = ensure_screenshot_dir(settings)
    metadata = {
        "screenshot_id": screenshot_id,
        "path": capture.get("path"),
        "sha256": capture.get("sha256"),
        "width": capture.get("width"),
        "height": capture.get("height"),
        "display_id": capture.get("display_id"),
        "created_at": capture.get("created_at")
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "node_id": node_id,
        "helper_version": capture.get("helper_version"),
        "bytes": capture.get("bytes"),
    }
    metadata_path = screenshot_dir / f"{screenshot_id}.json"
    _write_metadata_atomic(metadata_path, metadata)
    return metadata_path


def capture_screenshot_once(settings: Settings) -> dict:
    """Capture one screenshot via the local helper CLI. Read-only, local-only."""
    helper_error = helper_configuration_error(settings)
    if helper_error:
        logger.info("desktop observe skipped: %s", helper_error)
        if helper_error == "screenshot_helper_path_not_absolute":
            return {
                "ok": False,
                "error": helper_error,
                "message": "CONVEYOR_DESKTOP_SCREENSHOT_HELPER must be an absolute path.",
            }
        return {
            "ok": False,
            "error": helper_error,
            "message": "Desktop screenshot helper is not configured.",
        }

    helper = str(resolve_helper_path(settings))
    if settings.conveyor_desktop_screenshot_allow_upload:
        logger.warning("CONVEYOR_DESKTOP_SCREENSHOT_ALLOW_UPLOAD=true ignored in P5.2")

    screenshot_dir = ensure_screenshot_dir(settings)

    screenshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_path = (screenshot_dir / f"{screenshot_id}.png").resolve()

    if not str(output_path).startswith("/"):
        return {
            "ok": False,
            "error": "relative_output_path",
            "message": "Refusing relative screenshot output path.",
        }

    cmd = [
        helper,
        "--mode", "full-display",
        "--display", "main",
        "--output", str(output_path),
        "--json",
    ]

    logger.info(
        "desktop observe: invoking helper for screenshot_id=%s path=%s",
        screenshot_id,
        output_path,
    )

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "screenshot_helper_not_found",
            "message": "Desktop screenshot helper executable was not found.",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "screenshot_helper_timeout",
            "message": "Desktop screenshot helper timed out.",
        }

    stdout = (completed.stdout or "").strip()
    if not stdout:
        return {
            "ok": False,
            "error": "helper_empty_output",
            "message": "Desktop screenshot helper returned no JSON.",
        }

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "helper_invalid_json",
            "message": "Desktop screenshot helper returned invalid JSON.",
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "helper_invalid_json",
            "message": "Desktop screenshot helper returned invalid JSON.",
        }

    validated = validate_helper_payload(
        payload,
        expected_output_path=output_path,
        max_bytes=settings.conveyor_desktop_screenshot_max_bytes,
    )
    if not validated.get("ok"):
        logger.info(
            "desktop observe failed: error=%s screenshot_id=%s",
            validated.get("error"),
            screenshot_id,
        )
        return validated

    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    metadata_path = write_screenshot_metadata(
        settings,
        screenshot_id=screenshot_id,
        capture=validated,
        node_id=node_id,
    )

    logger.info(
        "desktop observe succeeded: screenshot_id=%s metadata=%s bytes=%s",
        screenshot_id,
        metadata_path,
        validated.get("bytes"),
    )

    result = {
        "ok": True,
        "screenshot_id": screenshot_id,
        "metadata_path": str(metadata_path),
        "path": validated["path"],
        "sha256": validated["sha256"],
        "width": validated.get("width"),
        "height": validated.get("height"),
        "display_id": validated.get("display_id"),
        "created_at": validated.get("created_at"),
        "helper_version": validated.get("helper_version"),
        "bytes": validated.get("bytes"),
        "uploaded": False,
    }
    return result