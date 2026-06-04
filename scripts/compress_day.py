#!/usr/bin/env python3
"""Compress the most recent daily worktree's MEMORY.md into ~/.codex/JOURNAL/.

Designed to be called hourly by codex-telegram-maintain.timer. The script is
gated on:
  - now.hour >= 12 (skip morning hours; only run in the afternoon and later)
  - last_compress_date < candidate (we have not already archived this day)

 Before writing the journal file, every line under "## unfiled" in the day's
 MEMORY.md is re-classified by CodexRunner.reclassify_unfiled (which reuses
 classify_memo). Moved lines land in their real section in the archive;
 lines that still won't classify stay under "## unfiled" so nothing is lost.

Pass --force to skip the hour gate (for manual backfills).
"""
from __future__ import annotations

import asyncio
import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings
from runner import CodexRunner

STATE_FILENAME = "last_compress_date.txt"
COMPRESS_HOUR_LOCAL = 12


def _user_now(settings: Settings) -> datetime:
    tz_name = settings.user_timezone
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def _read_state_date(memory_root: Path) -> datetime | None:
    state_file = memory_root / "state" / STATE_FILENAME
    if not state_file.exists():
        return None
    raw = state_file.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _write_state_date(memory_root: Path, stamp: datetime) -> None:
    state_dir = memory_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / STATE_FILENAME
    tmp = state_file.with_suffix(".txt.tmp")
    tmp.write_text(stamp.strftime("%Y-%m-%d") + "\n", encoding="utf-8")
    tmp.replace(state_file)


def _candidate_day(worktrees_root: Path, today: datetime) -> datetime | None:
    """Most recent daily worktree strictly before today, or None."""
    if not worktrees_root.exists():
        return None
    dates: list[datetime] = []
    for wt in worktrees_root.iterdir():
        if not wt.is_dir():
            continue
        prefix = CodexRunner.DAILY_WORKTREE_PREFIX
        if not wt.name.startswith(prefix):
            continue
        stamp_str = wt.name[len(prefix):]
        try:
            d = datetime.strptime(stamp_str, CodexRunner.DAILY_WORKTREE_FORMAT)
        except ValueError:
            continue
        if d.date() < today.date():
            dates.append(d)
    if not dates:
        return None
    return max(dates)


async def compress_if_needed(settings: Settings, *, force: bool = False) -> str:
    """Idempotent hourly entry point. Returns a one-line summary."""
    now = _user_now(settings)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    worktrees_root = settings.codex_task_root / "worktrees"
    candidate = _candidate_day(worktrees_root, today)
    last = _read_state_date(settings.codex_memory_root)

    if last is not None and last.date() >= today.date():
        return f"compress: already ran today ({last.strftime('%Y-%m-%d')}); skipping."
    if candidate is None:
        _write_state_date(settings.codex_memory_root, today)
        return f"compress: no prior day worktree to archive; marked {today.strftime('%Y-%m-%d')}."
    if last is not None and last.date() >= candidate.date():
        _write_state_date(settings.codex_memory_root, today)
        return (
            f"compress: last archive ({last.strftime('%Y-%m-%d')}) already covers "
            f"candidate {candidate.strftime('%Y-%m-%d')}; marked {today.strftime('%Y-%m-%d')}."
        )
    if not force and now.hour < COMPRESS_HOUR_LOCAL:
        return (
            f"compress: {candidate.strftime('%Y-%m-%d')} pending, "
            f"but local hour is {now.hour} (< {COMPRESS_HOUR_LOCAL}); waiting."
        )

    return await _archive_day(settings, candidate, now)


async def _archive_day(settings: Settings, day: datetime, now: datetime) -> str:
    worktree = settings.codex_task_root / "worktrees" / (
        CodexRunner.DAILY_WORKTREE_PREFIX + day.strftime(CodexRunner.DAILY_WORKTREE_FORMAT)
    )
    memory = worktree / CodexRunner.MEMORY_FILENAME
    if not worktree.exists():
        _write_state_date(settings.codex_memory_root, now.replace(hour=0, minute=0, second=0, microsecond=0))
        return f"compress: worktree for {day.strftime('%Y-%m-%d')} missing; marked today."
    if not memory.exists():
        _write_state_date(settings.codex_memory_root, now.replace(hour=0, minute=0, second=0, microsecond=0))
        return f"compress: {day.strftime('%Y-%m-%d')} worktree has no MEMORY.md; marked today."

    content = memory.read_text(encoding="utf-8", errors="replace").strip()
    if not content:
        memory.unlink()
        _write_state_date(settings.codex_memory_root, now.replace(hour=0, minute=0, second=0, microsecond=0))
        return f"compress: {day.strftime('%Y-%m-%d')} MEMORY.md was empty; removed and marked today."

    # Re-classify every "## unfiled" line before archiving so the journal
    # lands clean. Reuses CodexRunner.classify_memo, which falls back to
    # "unfiled" on any failure (so this never raises on a network blip).
    runner = CodexRunner(settings)
    archive_content, moved = await runner.reclassify_unfiled(content)

    journal_dir = settings.codex_memory_root / "JOURNAL"
    journal_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir = settings.codex_memory_root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    day_stamp = day.strftime("%Y-%m-%d")
    snapshot_target = snapshots_dir / f"{day_stamp}-{now.strftime('%H%M')}"
    if snapshot_target.exists():
        shutil.rmtree(snapshot_target, ignore_errors=True)
    shutil.copytree(worktree, snapshot_target, symlinks=True, ignore_dangling_symlinks=True)

    journal_file = journal_dir / f"{day_stamp}.md"
    reclass_line = f"- reclassified: {moved} unfiled -> proper sections\n" if moved else ""
    header = (
        f"# MEMORY.md archive — {day_stamp}\n\n"
        f"- worktree: `{worktree}`\n"
        f"- snapshot: `{snapshot_target}`\n"
        f"- archived_at: {now.isoformat()}\n"
        f"- bytes: {len(archive_content.encode('utf-8'))}\n"
        f"{reclass_line}\n"
    )
    journal_file.write_text(header + archive_content + "\n", encoding="utf-8")

    archived_name = f"{CodexRunner.MEMORY_FILENAME}.archived-{day_stamp}"
    archived_path = worktree / archived_name
    if archived_path.exists():
        archived_path.unlink()
    memory.rename(archived_path)

    _write_state_date(settings.codex_memory_root, now.replace(hour=0, minute=0, second=0, microsecond=0))
    moved_note = f", reclassified {moved} unfiled" if moved else ""
    return (
        f"compress: archived {day_stamp}{moved_note} -> {journal_file} (snapshot {snapshot_target.name}, "
        f"{len(archive_content.encode('utf-8'))} bytes); marked today."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--force", action="store_true", help="Skip the noon hour gate")
    args = parser.parse_args()

    settings = load_settings(args.env)
    summary = asyncio.run(compress_if_needed(settings, force=args.force))
    print(summary)


if __name__ == "__main__":
    main()
