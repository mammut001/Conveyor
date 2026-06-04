from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_job_metadata(job_dir: Path) -> dict[str, Any] | None:
    metadata_path = job_dir / "job.json"
    if not metadata_path.exists():
        return None
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def metadata_text(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    return value if isinstance(value, str) else default


def parse_metadata_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_job_id_time(job_id: str) -> datetime | None:
    try:
        return datetime.strptime(job_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def job_sort_time(job_dir: Path) -> datetime:
    data = load_job_metadata(job_dir)
    if data:
        for key in ("finished_at", "started_at"):
            parsed = parse_metadata_time(data.get(key))
            if parsed:
                return parsed
    parsed_id = parse_job_id_time(job_dir.name)
    if parsed_id:
        return parsed_id
    return datetime.fromtimestamp(job_dir.stat().st_mtime, timezone.utc)
