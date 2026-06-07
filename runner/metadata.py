"""runner/metadata.py — split out of runner.py.

The original runner.py was 2005 lines and 5 big
responsibilities. This file is one slice.

runner/core.py attaches each function on this module
to the CodexRunner class as a method at import time,
so callers see the same public surface.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# Module-level constants (also on CodexRunner class shell)

MEMORY_FILENAME = "MEMORY.md"
from config import Settings, load_settings
from runner.types import Job, JobMode, JobRecord
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
def _read_final_message(self, job: Job) -> str:
    if job.final_message_path and job.final_message_path.exists():
        return truncate(job.final_message_path.read_text(encoding="utf-8", errors="replace"), 3000)
    return ""


def _write_job_metadata(self, job: Job) -> None:
    if not job.metadata_path:
        return
    job.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    duration_end = job.finished_at or datetime.now(timezone.utc)
    duration_seconds = max(0, int((duration_end - job.started_at).total_seconds()))
    data = {
        "id": job.id,
        "mode": job.mode.value,
        "sandbox": job.sandbox,
        "state": job.state.value,
        "started_at": job.started_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_seconds": duration_seconds,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "return_code": job.return_code,
        "rate_limited": job.rate_limited,
        "usage": job.usage,
        "cancel_requested": job.cancel_requested,
        "worktree_path": str(job.worktree_path) if job.worktree_path else None,
        "log_path": str(job.log_path) if job.log_path else None,
        "final_message_path": str(job.final_message_path) if job.final_message_path else None,
        "last_event": redact_text(truncate(job.last_event, 1200)),
        "error": redact_text(truncate(job.error, 1200)) if job.error else "",
        "summary": redact_text(truncate(job.summary, 1200)) if job.summary else "",
    }
    tmp_path = job.metadata_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(job.metadata_path)


def job_records(self, limit: int = 20) -> list[JobRecord]:
    logs_root = self.settings.codex_task_root / "logs"
    if not logs_root.exists():
        return []
    records: list[JobRecord] = []
    for log_dir in logs_root.iterdir():
        if not log_dir.is_dir():
            continue
        job_id = log_dir.name
        final_file = self._latest_file(log_dir, "attempt-*-final.txt")
        attempt_file = self._latest_file(log_dir, "attempt-*.jsonl") or (log_dir / "codex.jsonl" if (log_dir / "codex.jsonl").exists() else None)
        metadata = load_job_metadata(log_dir)
        final_preview = ""
        summary = metadata_text(metadata, "summary") if metadata else ""
        if summary:
            final_preview = summary.strip().replace("\n", " ")
        elif final_file:
            final_preview = final_file.read_text(encoding="utf-8", errors="replace").strip().replace("\n", " ")
        state = "unknown"
        metadata_state = metadata_text(metadata, "state") if metadata else ""
        if metadata_state:
            state = metadata_state
        elif attempt_file:
            state = self._state_from_attempt_file(attempt_file)
        if final_preview and state == "unknown":
            state = "completed"
        worktree_path = None
        if metadata:
            wt_str = metadata_text(metadata, "worktree_path")
            if wt_str:
                wt = Path(wt_str)
                if wt.exists():
                    worktree_path = wt
        if worktree_path is None:
            # Fall back to legacy per-job path for jobs created before the daily worktree switch.
            legacy_path = self.settings.codex_task_root / "worktrees" / job_id
            if legacy_path.exists():
                worktree_path = legacy_path
        updated_at = job_sort_time(log_dir)
        mode_value = "unknown"
        if metadata:
            mv = metadata_text(metadata, "mode")
            if mv:
                mode_value = mv
        records.append(
            JobRecord(
                id=job_id,
                state=state,
                mode=mode_value,
                final_preview=truncate(final_preview, 180) if final_preview else "",
                log_dir=log_dir,
                worktree_path=worktree_path,
                updated_at=updated_at,
            )
        )
    return sorted(records, key=lambda record: record.updated_at, reverse=True)[:limit]


def _last_job_id(self) -> str:
    if self.last_job:
        return self.last_job.id
    records = self.job_records(1)
    return records[0].id if records else "(none)"


def _last_worktree_path(self) -> Path | None:
    if self.last_job and self.last_job.worktree_path:
        return self.last_job.worktree_path
    records = self.job_records(1)
    if not records:
        return None
    return records[0].worktree_path


def _latest_file(self, directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime)
    return matches[-1] if matches else None


def _state_from_attempt_file(self, attempt_file: Path) -> str:
    state = "running"
    for line in attempt_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "turn.completed":
            state = "completed"
        elif event_type == "turn.failed":
            state = "failed"
    return state
