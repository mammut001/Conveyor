"""runner/operators/maintain.py — split out of runner.py.

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
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
async def clean_old_jobs(self, keep: int = 20) -> str:
    # Per-job log dirs only. Daily worktrees are shared across jobs, so
    # they are cleaned separately by clean_old_worktrees().
    records = self.job_records(10000)
    if keep < 1:
        raise ValueError("keep must be at least 1")
    stale = records[keep:]
    removed_logs = 0
    for record in stale:
        if record.log_dir.exists():
            shutil.rmtree(record.log_dir, ignore_errors=True)
            removed_logs += 1
    return f"Cleaned {removed_logs} log dirs. Kept {min(len(records), keep)} recent jobs."


async def clean_old_worktrees(self, keep_days: int = 7) -> str:
    worktrees_root = self.settings.codex_task_root / "worktrees"
    if not worktrees_root.exists():
        return "No worktrees to clean."
    today_str = self._user_today().strftime(self.DAILY_WORKTREE_FORMAT)
    daily: list[tuple[date, Path]] = []
    legacy: list[Path] = []
    for wt in worktrees_root.iterdir():
        if not wt.is_dir() or wt.name == f"{self.DAILY_WORKTREE_PREFIX}{today_str}":
            continue
        m = re.match(rf"{self.DAILY_WORKTREE_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}})$", wt.name)
        if m:
            try:
                d = datetime.strptime(m.group(1), self.DAILY_WORKTREE_FORMAT).date()
                daily.append((d, wt))
            except ValueError:
                legacy.append(wt)
        else:
            legacy.append(wt)
    daily.sort(key=lambda x: x[0], reverse=True)
    keep = daily[:keep_days]
    remove = daily[keep_days:]
    removed_legacy = 0
    for wt in legacy:
        await self._remove_worktree(wt)
        removed_legacy += 1
    removed_daily = 0
    skipped_uncompressed = 0
    for _, wt in remove:
        if (wt / MEMORY_FILENAME).exists():
            skipped_uncompressed += 1
            continue
        await self._remove_worktree(wt)
        removed_daily += 1
    msg = (
        f"Cleaned {removed_legacy} legacy worktrees and {removed_daily} old daily worktrees "
        f"(kept last {keep_days} days including today)."
    )
    if skipped_uncompressed:
        msg += f" Skipped {skipped_uncompressed} uncompressed (still has {MEMORY_FILENAME}; run compress to archive)."
    return msg
