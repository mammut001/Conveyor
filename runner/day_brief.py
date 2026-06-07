"""runner/day_brief.py — split out of runner.py.

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

DAY_BRIEF_STATE_FILENAME = "last_day_brief.txt"
DAY_BRIEF_PREVIEW_CHARS = 500
DAY_BRIEF_RECENT_JOBS = 3
DAILY_WORKTREE_FORMAT = "%Y-%m-%d"
from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
def _day_brief_state_path(self) -> Path:
    return self.settings.codex_memory_root / "state" / DAY_BRIEF_STATE_FILENAME


def _day_brief_recent_jobs(self, limit: int = 3) -> list[str]:
    records = self.job_records(limit=limit)
    if not records:
        return []
    lines: list[str] = []
    for record in records:
        preview = record.final_preview or "(no preview)"
        stamp = record.updated_at.strftime(DAILY_WORKTREE_FORMAT)
        lines.append(f"- {stamp} {record.id} {record.state} {preview}")
    return lines


def _day_brief_text(self) -> str:
    # Onboarding-B. Day-brief: warm-start the agent for the first
    # job of each user-local day with a snapshot of yesterday's
    # journal, today's MEMORY.md, and the most recent job
    # summaries. Subsequent jobs the same day get "" (no brief)
    # so we don't repeat the recap on every message. State is a
    # one-line date stamp in codex_memory_root/state/ written on
    # the first deliver; checked at the top of every call.
    # Failure to read or write the state file is non-fatal: the
    # brief is still delivered this call, and the next call will
    # simply redeliver (a duplicate brief is harmless; a missing
    # brief is a cold start, which is what we're trying to avoid).
    from datetime import timedelta
    today = self._user_today()
    today_str = today.strftime(DAILY_WORKTREE_FORMAT)
    state_path = self._day_brief_state_path()
    last_brief_date = ""
    if state_path.exists():
        try:
            last_brief_date = state_path.read_text(encoding="utf-8").strip()
        except OSError:
            last_brief_date = ""
    if last_brief_date == today_str:
        return ""
    yesterday = today - timedelta(days=1)
    yesterday_str = yesterday.strftime(DAILY_WORKTREE_FORMAT)
    yesterday_journal = self.settings.codex_memory_root / "JOURNAL" / f"{yesterday_str}.md"
    yesterday_text = "(no journal)"
    if yesterday_journal.exists():
        try:
            content = yesterday_journal.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            content = ""
        if content:
            snippet = content[: DAY_BRIEF_PREVIEW_CHARS]
            if len(content) > DAY_BRIEF_PREVIEW_CHARS:
                snippet += "..."
            yesterday_text = snippet
    today_memory = self.today_memory_text()
    if today_memory:
        snippet = today_memory[: DAY_BRIEF_PREVIEW_CHARS]
        if len(today_memory) > DAY_BRIEF_PREVIEW_CHARS:
            snippet += "..."
        today_memory = snippet
    else:
        today_memory = "(empty)"
    recent_jobs = self._day_brief_recent_jobs(limit=DAY_BRIEF_RECENT_JOBS)
    recent_block = "\n".join(recent_jobs) if recent_jobs else "(none)"
    brief = (
        f'<day-brief date="{today_str}" first-job-of-day="true">\n'
        f"## Yesterday's journal ({yesterday_str})\n"
        f"{yesterday_text}\n\n"
        f"## Today's MEMORY.md\n"
        f"{today_memory}\n\n"
        f"## Recent jobs (last {DAY_BRIEF_RECENT_JOBS})\n"
        f"{recent_block}\n"
        f"</day-brief>\n\n"
    )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(today_str, encoding="utf-8")
    except OSError:
        pass
    return brief
