"""runner/operators/jobs.py — split out of runner.py.

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
def status_text(self) -> str:
    job = self.current_job or self.last_job
    if not job:
        return "No jobs yet."
    elapsed = self._elapsed(job)
    parts = [
        f"Job: {job.id}",
        f"Mode: /{job.mode.value}",
        f"State: {job.state.value}",
        f"Sandbox: {job.sandbox}",
        f"Attempt: {job.attempt}/{job.max_attempts}",
        f"Elapsed: {elapsed}",
        f"Last event: {truncate(job.last_event, 500)}",
    ]
    if job.log_path:
        parts.append(f"Log: {job.log_path}")
    if job.summary:
        parts.append(f"Summary: {truncate(job.summary, 1200)}")
    if job.error:
        parts.append(f"Error: {truncate(job.error, 1200)}")
    return "\n".join(parts)


async def diff_text(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    if not worktree_path or not worktree_path.exists():
        return "No job worktree available yet."
    status = await self._git(["status", "--short"], cwd=worktree_path, check=False)
    stat = await self._git(["diff", "--stat"], cwd=worktree_path, check=False)
    diff = await self._git(["diff", "--", "."], cwd=worktree_path, check=False)
    if not status.strip() and not stat.strip() and not diff.strip():
        return f"Job {job_id}: no git diff."
    return truncate(
        f"Job {job_id} status:\n{status.strip() or '(clean)'}\n\n"
        f"Diff stat:\n{stat.strip() or '(no tracked changes)'}\n\n"
        f"Diff preview:\n{diff.strip() or '(no tracked diff; check untracked files above)'}",
        3900,
    )


def jobs_text(self, limit: int = 8) -> str:
    records = self.job_records(limit)
    if not records:
        return "No jobs yet."
    lines = ["Recent jobs:"]
    for record in records:
        preview = f" — {record.final_preview}" if record.final_preview else ""
        lines.append(f"{record.id} · {record.state}{preview}")
    return truncate("\n".join(lines), 3900)


def last_text(self) -> str:
    record = self.job_records(1)
    if not record:
        return "No jobs yet."
    item = record[0]
    if item.final_preview:
        return item.final_preview
    return f"{item.id}: {item.state}"


async def discard_last_job(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    if not worktree_path or not worktree_path.exists():
        return "No job worktree to discard."
    await self._remove_worktree(worktree_path)
    return f"Discarded worktree for {job_id}."


async def apply_last_job(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    if not worktree_path or not worktree_path.exists():
        return "No job worktree to apply."

    root_status = await self._git(["status", "--short"], cwd=self.settings.codex_workspace_root, check=False)
    if root_status.strip():
        return "Main workspace has uncommitted changes. I will not apply over a dirty repo."

    status = await self._git(["status", "--short"], cwd=worktree_path, check=False)
    # Exclude MEMORY.md from the diff we apply; it is per-day working memory
    # that should never be merged into the main repo.
    memory_pathspec = f":(exclude){MEMORY_FILENAME}"
    status_no_memory = await self._git(
        ["status", "--short", "--", ".", memory_pathspec], cwd=worktree_path, check=False
    )
    if not status_no_memory.strip():
        return f"Job {job_id} has no changes to apply."

    patch = await self._git(
        ["diff", "--binary", "HEAD", "--", ".", memory_pathspec], cwd=worktree_path, check=False
    )
    if patch.strip():
        process = await asyncio.create_subprocess_exec(
            "git",
            "apply",
            "--binary",
            "-",
            cwd=self.settings.codex_workspace_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(patch.encode("utf-8"))
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            return f"Could not apply tracked diff for {job_id}: {truncate(detail, 1200)}"

    copied = await self._copy_untracked_files(worktree_path)
    return f"Applied {job_id}. Copied {copied} new files. Review main repo before committing."
