"""runner/types.py — split out of runner.py.

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

from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text

from enum import Enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable
ProgressCallback = Callable[[str], Awaitable[None]]


class JobMode(str, Enum):
    RUN = "run"
    FIX = "fix"
    # MEMO used to live here. The memo path bypasses codex entirely now
    # (see _handle_memo_fast_path in bot.py), so there is no codex-side
    # mode to route to. MEMORY.md is written by the runner's own helpers.

    @property
    def sandbox(self) -> str:
        # Full host access: Codex runs with danger-full-access so shell can
        # reach ~/.bashrc, /opt/conveyor, etc. on the bot VPS. Outer gate is
        # still Telegram/Feishu allowlist + explicit /apply for git merges.
        return "danger-full-access"

    @property
    def stdin_prefix(self) -> str:
        label = "chat" if self is JobMode.RUN else "fix"
        return (
            f"[mode: {label} | sandbox: danger-full-access | network on | "
            "shell and file access on the bot host]\n\n"
        )


class JobState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    mode: JobMode
    prompt: str
    sandbox: str
    state: JobState = JobState.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    return_code: int | None = None
    worktree_path: Path | None = None
    log_path: Path | None = None
    final_message_path: Path | None = None
    metadata_path: Path | None = None
    summary: str = ""
    last_event: str = "starting"
    error: str = ""
    attempt: int = 0
    max_attempts: int = 1
    rate_limited: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)


@dataclass(frozen=True)
class JobRecord:
    id: str
    state: str
    mode: str
    final_preview: str
    log_dir: Path
    worktree_path: Path | None
    updated_at: datetime
