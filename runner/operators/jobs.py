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
    from runner.file_lock import file_lock
    from runner.apply_policy import validate_apply_paths, collect_tracked_changed_files, collect_untracked_files

    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    if not worktree_path or not worktree_path.exists():
        return "No job worktree to apply."

    lock_path = self.settings.codex_task_root / "locks" / "apply.lock"
    with file_lock(lock_path):
        root_status = await self._git(["status", "--short"], cwd=self.settings.codex_workspace_root, check=False)
        if root_status.strip():
            return "Main workspace has uncommitted changes. I will not apply over a dirty repo."

        # Collect tracked changed files. Fail closed: a collection error
        # must never look like "no paths to validate".
        tracked_result = collect_tracked_changed_files(worktree_path)
        if not tracked_result.ok:
            return f"Refused to apply job {job_id}: could not collect changed files safely."
        # Collect untracked files, same fail-closed contract.
        untracked_result = collect_untracked_files(worktree_path)
        if not untracked_result.ok:
            return f"Refused to apply job {job_id}: could not collect changed files safely."

        tracked_files = tracked_result.paths
        untracked_files = untracked_result.paths

        # Validate tracked paths
        if tracked_files:
            val_tracked = validate_apply_paths(tracked_files, kind="tracked", settings=self.settings, worktree_path=worktree_path)
            if not val_tracked.allowed:
                return f"Refused to apply job {job_id}: blocked high-risk paths: {val_tracked.reason}"

        # Validate untracked paths
        if untracked_files:
            val_untracked = validate_apply_paths(untracked_files, kind="untracked", settings=self.settings, worktree_path=worktree_path)
            if not val_untracked.allowed:
                return f"Refused to apply job {job_id}: blocked high-risk paths: {val_untracked.reason}"

        # Snapshot the validated untracked set so we can detect any drift
        # before the copy step (TOCTOU inside the worktree).
        validated_untracked = set(untracked_files)

        # Exclude MEMORY.md from the diff we apply; it is per-day working memory
        # that should never be merged into the main repo.
        memory_pathspec = f":(exclude){MEMORY_FILENAME}"
        status_no_memory = await self._git(
            ["status", "--short", "--", ".", memory_pathspec], cwd=worktree_path, check=False
        )
        # Also verify if there are actually any tracked or untracked changes to apply
        has_tracked = len(tracked_files) > 0
        has_untracked = len(untracked_files) > 0
        if not has_tracked and not has_untracked:
            return f"Job {job_id} has no changes to apply."

        patch = await self._git(
            ["diff", "--binary", "HEAD", "--", ".", memory_pathspec], cwd=worktree_path, check=False
        )
        if patch.strip():
            # Recheck dirty main repo (TOCTOU safety)
            root_status_pre = await self._git(["status", "--short"], cwd=self.settings.codex_workspace_root, check=False)
            if root_status_pre.strip():
                return "Main workspace has uncommitted changes. I will not apply over a dirty repo."

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

        # Recheck the untracked list immediately before copy, while apply.lock
        # is held. If the set changed since validation (a new untracked file
        # appeared, or one disappeared), refuse: the validated snapshot no
        # longer matches the worktree, so copying could let an unvalidated
        # file through or miss one the user reviewed in /diff.
        recheck_result = collect_untracked_files(worktree_path)
        if not recheck_result.ok:
            return f"Refused to apply job {job_id}: could not collect changed files safely."
        current_untracked = set(recheck_result.paths)
        if current_untracked != validated_untracked:
            return (
                f"Refused to apply job {job_id}: untracked files changed during apply. "
                "Please rerun /diff and /apply."
            )

        # Copy only the validated untracked files. The safe helper does NOT
        # re-list the worktree, so a file that appeared between the recheck
        # above and the copy cannot slip in.
        copied = await self._copy_validated_untracked_files(worktree_path, list(validated_untracked))

        status_summary = await self._git(["status", "--short"], cwd=self.settings.codex_workspace_root, check=False)
        safe_summary = redact_text(status_summary.strip())

        return (
            f"Applied {job_id}. Copied {copied} new files. Review main repo before committing.\n\n"
            f"Workspace status:\n{safe_summary}"
        )
