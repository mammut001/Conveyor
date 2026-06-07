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
        # /run is a read-only Q&A path; /fix lets the model edit the workspace.
        return "read-only" if self is JobMode.RUN else "workspace-write"

    @property
    def stdin_prefix(self) -> str:
        # One short hint line, so the model knows its sandbox before it starts.
        # /run is a read-only Q&A path; the codex CLI's read-only sandbox
        # does NOT disable web tools (search/fetch) — the old "no network"
        # clause in this prompt was the only thing blocking them. We allow
        # web tools in /run so plain chat can answer "what's AAPL at"
        # without forcing the user to type /fix. Writes and shell still
        # require /fix (workspace-write), so the security boundary is
        # unchanged.
        if self is JobMode.RUN:
            return (
                "[mode: run | sandbox: read-only | network on, no writes | "
                "web tools allowed, file/shell writes still need /fix]\n\n"
            )
        return (
            "[mode: fix | sandbox: workspace-write | network on | "
            "you may read and write inside the workspace]\n\n"
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
