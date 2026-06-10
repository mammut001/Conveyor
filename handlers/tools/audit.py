"""handlers/tools/audit.py — JSONL audit log for WRITE/DESTRUCTIVE tools."""
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


def audit_log_path(settings: Settings) -> Path:
    return settings.codex_memory_root / "audit" / AUDIT_FILENAME


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
    """Append one JSONL record. Never raises."""
    try:
        path = audit_log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
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
