"""handlers/tools/audit.py — JSONL audit log for WRITE/DESTRUCTIVE tools.

Includes size-based rotation: when tools.log exceeds AUDIT_MAX_BYTES,
it is rotated to tools.log.1, tools.log.1 → tools.log.2, etc., up to
AUDIT_MAX_ROTATED files.  This keeps the audit directory bounded
without losing history.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

AUDIT_FILENAME = "tools.log"
PREVIEW_LIMIT = 400
# Rotation defaults (bytes and file count).
AUDIT_MAX_BYTES = 1 * 1024 * 1024   # 1 MB
AUDIT_MAX_ROTATED = 3                # keep tools.log.1 … .3


def audit_log_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "audit" / AUDIT_FILENAME


def _rotate_if_needed(path: Path) -> None:
    """Rotate the log file if it exceeds AUDIT_MAX_BYTES.

    Rotation scheme:  .3 is deleted, .2 → .3, .1 → .2, current → .1.
    This is intentionally simple and avoids external dependencies.
    """
    try:
        if not path.is_file():
            return
        if path.stat().st_size < AUDIT_MAX_BYTES:
            return
        parent = path.parent
        # Drop the oldest rotated file.
        oldest = parent / f"{AUDIT_FILENAME}.{AUDIT_MAX_ROTATED}"
        oldest.unlink(missing_ok=True)
        # Shift existing rotated files up by one.
        for i in range(AUDIT_MAX_ROTATED - 1, 0, -1):
            src = parent / f"{AUDIT_FILENAME}.{i}"
            dst = parent / f"{AUDIT_FILENAME}.{i + 1}"
            if src.exists():
                src.rename(dst)
        # Rotate the current log.
        path.rename(parent / f"{AUDIT_FILENAME}.1")
    except OSError:
        logger.exception("Failed to rotate audit log")


def audit_tool_event(
    settings: Settings,
    *,
    operator_id: str,
    chat_id: str,
    channel: str,
    tool_name: str,
    arg: str,
    danger: str,
    action: str,
    result_preview: str = "",
    error_preview: str = "",
) -> None:
    """Append one JSONL record.  Rotates before writing if needed.  Never raises."""
    try:
        path = audit_log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operator_id": operator_id,
            "chat_id": chat_id,
            "channel": channel,
            "tool_name": tool_name,
            "arg": truncate(redact_text(arg or ""), PREVIEW_LIMIT),
            "danger": danger,
            "action": action,
        }
        if result_preview:
            record["result_preview"] = truncate(redact_text(result_preview), PREVIEW_LIMIT)
        if error_preview:
            record["error_preview"] = truncate(redact_text(error_preview), PREVIEW_LIMIT)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write tool audit log")


def read_audit_tail(settings: Settings, n: int = 10) -> list[dict[str, Any]]:
    """Read the last N records from the current (non-rotated) log."""
    path = audit_log_path(settings)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def rotated_log_paths(settings: Settings) -> list[Path]:
    """Return paths to rotated log files (tools.log.1, .2, …) that exist."""
    parent = audit_log_path(settings).parent
    paths: list[Path] = []
    for i in range(1, AUDIT_MAX_ROTATED + 1):
        p = parent / f"{AUDIT_FILENAME}.{i}"
        if p.is_file():
            paths.append(p)
    return paths
