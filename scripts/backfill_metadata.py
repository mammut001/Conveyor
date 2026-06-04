#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings
from redaction import redact_text, truncate
from scripts.harness_common import latest_attempt_file, latest_final_file


RATE_LIMIT_RE = re.compile(r"\b429\b|too many requests|rate limit|high demand", re.IGNORECASE)


def _latest_attempt_or_legacy(job_dir: Path) -> Path | None:
    return latest_attempt_file(job_dir) or (job_dir / "codex.jsonl" if (job_dir / "codex.jsonl").exists() else None)


def _parse_job_id_time(job_id: str) -> datetime | None:
    stamp = job_id[:15]
    try:
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_preview(event: dict) -> str:
    event_type = str(event.get("type") or event.get("event") or "event")
    for key in ("message", "summary", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return f"{event_type}: {value.strip()}"
    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type") or item.get("kind") or "item"
        text = item.get("text") or item.get("summary") or item.get("message")
        if isinstance(text, str) and text.strip():
            return f"{event_type}/{item_type}: {text.strip()}"
    return f"{event_type}: {json.dumps(event, ensure_ascii=False)[:500]}"


def _read_attempt(attempt: Path | None) -> tuple[str, dict[str, int], bool, str, str]:
    state = "unknown"
    usage: dict[str, int] = {}
    rate_limited = False
    last_event = ""
    error = ""
    if not attempt or not attempt.exists():
        return state, usage, rate_limited, last_event, error

    state = "running"
    for raw in attempt.read_text(encoding="utf-8", errors="replace").splitlines():
        if RATE_LIMIT_RE.search(raw):
            rate_limited = True
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        preview = _event_preview(event)
        if preview:
            last_event = preview
        if event_type == "turn.completed":
            state = "completed"
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = {key: int(value) for key, value in raw_usage.items() if isinstance(value, int)}
        elif event_type == "turn.failed":
            state = "failed"
        elif event_type == "error":
            error = preview
    return state, usage, rate_limited, last_event, error


def _latest_attempt_number(job_dir: Path) -> int:
    numbers: list[int] = []
    for path in job_dir.glob("attempt-*.jsonl"):
        match = re.match(r"attempt-(\d+)\.jsonl$", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers) if numbers else (1 if (job_dir / "codex.jsonl").exists() else 0)


def build_metadata(settings: Settings, job_dir: Path) -> dict:
    job_id = job_dir.name
    attempt = _latest_attempt_or_legacy(job_dir)
    final = latest_final_file(job_dir)
    state, usage, rate_limited, last_event, error = _read_attempt(attempt)
    summary = ""
    if final and final.exists():
        summary = final.read_text(encoding="utf-8", errors="replace").strip()
        if state in {"unknown", "running"} and summary:
            state = "completed"
    if state == "unknown" and error:
        state = "failed"

    started_at = _parse_job_id_time(job_id) or datetime.fromtimestamp(job_dir.stat().st_mtime, timezone.utc)
    finished_source = final or attempt or job_dir
    finished_at = datetime.fromtimestamp(finished_source.stat().st_mtime, timezone.utc)
    duration_seconds = max(0, int((finished_at - started_at).total_seconds()))
    worktree_path = settings.codex_task_root / "worktrees" / job_id

    return {
        "id": job_id,
        "mode": "unknown",
        "sandbox": "unknown",
        "state": state,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat() if state != "running" else None,
        "duration_seconds": duration_seconds,
        "attempt": _latest_attempt_number(job_dir),
        "max_attempts": 0,
        "return_code": 0 if state == "completed" else (1 if state == "failed" else None),
        "rate_limited": rate_limited,
        "usage": usage,
        "cancel_requested": False,
        "worktree_path": str(worktree_path) if worktree_path.exists() else None,
        "log_path": str(attempt) if attempt else None,
        "final_message_path": str(final) if final else None,
        "last_event": redact_text(truncate(last_event, 1200)) if last_event else "",
        "error": redact_text(truncate(error, 1200)) if error else "",
        "summary": redact_text(truncate(summary, 1200)) if summary else "",
        "backfilled": True,
    }


def backfill_job_metadata(settings: Settings, force: bool = False) -> int:
    logs_root = settings.codex_task_root / "logs"
    if not logs_root.exists():
        return 0
    written = 0
    for job_dir in sorted([path for path in logs_root.iterdir() if path.is_dir()]):
        metadata_path = job_dir / "job.json"
        if metadata_path.exists() and not force:
            continue
        data = build_metadata(settings, job_dir)
        tmp_path = metadata_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(metadata_path)
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing job.json sidecars from legacy Codex job logs.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--force", action="store_true", help="Rewrite existing job.json files too")
    args = parser.parse_args()
    settings = load_settings(args.env)
    written = backfill_job_metadata(settings, force=args.force)
    print(f"Backfilled {written} job metadata files.")


if __name__ == "__main__":
    main()
