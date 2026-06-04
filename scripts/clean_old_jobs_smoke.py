#!/usr/bin/env python3
"""Smoke tests for CodexRunner.clean_old_jobs.

Per-job log dirs under codex_task_root/logs/<job_id>/ accumulate as
the bot runs. auto_maintain sweeps them with `keep=50` (current
default) hourly when the count crosses --clean-threshold=30, so the
"30 < 50" early-warning signal survives a smoke regression. These
tests pin the selection down: keep boundary, keep>total, ValueError
guard, and the *ordering source* (metadata finished_at beats id-time).
The actual rmtree call is real (we create tmpdirs the OS can
unlink), no git workspace, no network.

Run with:  .venv/bin/python scripts/clean_old_jobs_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results


# ---- fixtures ------------------------------------------------------------

def _make_settings(tmp: Path) -> Settings:
    """Settings with codex_task_root under tmp; other paths are placeholders.

    codex_workspace_root, codex_memory_root, and the bot token don't
    matter: clean_old_jobs only reads codex_task_root/logs and writes
    nothing back. The fixture also pre-creates logs/ and worktrees/;
    CodexRunner.__init__ does NOT mkdir these (that's in validate()),
    so tests that don't call validate() need them upfront.
    """
    (tmp / "task" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "task" / "worktrees").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="fake",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp / "ws",
        codex_bin="codex",
        codex_task_root=tmp / "task",
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=20,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
    )


def _make_job_dir(settings: Settings, job_id: str, job_json: dict | None = None) -> Path:
    """Create a logs/<job_id>/ dir so job_records() will surface a record.

    job_id must start with YYYYMMDD-HHMMSS (15 chars) for parse_job_id_time
    to produce a deterministic updated_at; otherwise the test would be
    timezone/mtime-flaky. job_json, if given, is written as the
    metadata file load_job_metadata reads.
    """
    log_dir = settings.codex_task_root / "logs" / job_id
    log_dir.mkdir(parents=True, exist_ok=True)
    if job_json is not None:
        (log_dir / "job.json").write_text(
            json.dumps(job_json, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return log_dir


# ---- branch tests --------------------------------------------------------

async def _test_no_logs_root() -> CheckResult:
    """codex_task_root/logs does not exist -> 0 records, no rmtree, no raise."""
    name = "clean: logs_root missing -> no-op"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            # CodexRunner.__init__ mkdirs logs/; remove it so the function sees
            # the missing-root branch, not the empty-dir branch.
            logs = settings.codex_task_root / "logs"
            if logs.exists():
                for child in logs.iterdir():
                    child.rmdir()
                logs.rmdir()
            runner = CodexRunner(settings)
            summary = await runner.clean_old_jobs(keep=5)
            if "Cleaned 0 log dirs" not in summary or "Kept 0 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if logs.exists() and any(logs.iterdir()):
                return CheckResult(name, False, "missing-root branch left a logs/ artifact")
            return CheckResult(name, True, "missing root -> 0/0 with no raises")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_empty_logs_dir() -> CheckResult:
    """codex_task_root/logs exists but is empty -> 0 records, no rmtree."""
    name = "clean: empty logs dir -> no-op"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)
            # logs/ already exists and is empty from __init__; double-check.
            logs = settings.codex_task_root / "logs"
            if any(logs.iterdir()):
                return CheckResult(name, False, "test setup: logs not empty")
            summary = await runner.clean_old_jobs(keep=5)
            if "Cleaned 0 log dirs" not in summary or "Kept 0 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            return CheckResult(name, True, "empty dir -> 0/0 (no rmtree)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_keep_boundary() -> CheckResult:
    """5 jobs (id-time span 1s each), keep=1 -> newest 1 kept, oldest 4 removed."""
    name = "clean: keep=1 across 5 jobs drops the 4 oldest log dirs"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)

            ids = [f"20260101-12000{i}-job{i}" for i in range(5)]
            for jid in ids:
                _make_job_dir(settings, jid)

            summary = await runner.clean_old_jobs(keep=1)
            logs = settings.codex_task_root / "logs"
            kept = [p.name for p in logs.iterdir() if p.is_dir()]
            if kept != [ids[-1]]:
                return CheckResult(
                    name, False,
                    f"expected only {ids[-1]} to remain, got {kept}",
                )
            if "Cleaned 4 log dirs" not in summary or "Kept 1 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            return CheckResult(name, True, "kept newest, rmtree'd 4 oldest (id-time order)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_keep_equals_total() -> CheckResult:
    """5 jobs, keep=5 -> 0 stale, summary 'Kept 5 recent jobs'."""
    name = "clean: keep=5 == total leaves every log dir on disk"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)

            ids = [f"20260102-09000{i}-job{i}" for i in range(5)]
            for jid in ids:
                _make_job_dir(settings, jid)

            summary = await runner.clean_old_jobs(keep=5)
            logs = settings.codex_task_root / "logs"
            survivors = sorted(p.name for p in logs.iterdir() if p.is_dir())
            if survivors != sorted(ids):
                return CheckResult(
                    name, False,
                    f"survivor set mismatch.\nexpected={sorted(ids)}\nactual={survivors}",
                )
            if "Cleaned 0 log dirs" not in summary or "Kept 5 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            return CheckResult(name, True, "keep == total -> 0 rmtree, all dirs kept")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_keep_exceeds_total() -> CheckResult:
    """3 jobs, keep=10 -> 0 stale, 'Kept 3 recent jobs' (clamped to actual)."""
    name = "clean: keep=10 across 3 jobs is a no-op (keep > total)"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)

            ids = [f"20260301-15000{i}-job{i}" for i in range(3)]
            for jid in ids:
                _make_job_dir(settings, jid)

            summary = await runner.clean_old_jobs(keep=10)
            logs = settings.codex_task_root / "logs"
            survivors = sorted(p.name for p in logs.iterdir() if p.is_dir())
            if survivors != sorted(ids):
                return CheckResult(
                    name, False,
                    f"survivor set mismatch.\nexpected={sorted(ids)}\nactual={survivors}",
                )
            if "Cleaned 0 log dirs" not in summary or "Kept 3 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            return CheckResult(name, True, "keep > total -> 0 rmtree, all dirs kept (count clamped)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_keep_zero_raises() -> CheckResult:
    """keep<1 is the documented guard; a regression would silently wipe logs."""
    name = "clean: keep=0 raises ValueError (no silent full wipe)"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)
            for jid in ("20260401-080000-a", "20260401-080001-b"):
                _make_job_dir(settings, jid)
            try:
                await runner.clean_old_jobs(keep=0)
            except ValueError as exc:
                if "keep must be at least 1" not in str(exc):
                    return CheckResult(name, False, f"unexpected ValueError: {exc}")
                # Logs must still be on disk -- the guard fires before rmtree.
                logs = settings.codex_task_root / "logs"
                if len([p for p in logs.iterdir() if p.is_dir()]) != 2:
                    return CheckResult(name, False, "guard fired after rmtree ran")
                return CheckResult(name, True, "ValueError raised, no log dirs touched")
            return CheckResult(name, False, "expected ValueError, got none")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_metadata_finished_at_wins() -> CheckResult:
    """An older-id job with metadata finished_at beats a newer-id job without it.

    Without this guarantee, the function would sort by id-time only
    and could rmtree the most-recently-finished job in production.
    Job A's id suggests 12:00 on Jan 1, but its job.json says it
    finished 2026-06-04; Job B's id suggests 13:00 on Jan 1 with no
    metadata. After sort: A first (newer finished_at), B second.
    keep=1 -> A survives, B is removed.
    """
    name = "clean: metadata finished_at overrides id-time for ordering"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            runner = CodexRunner(settings)

            _make_job_dir(
                settings,
                "20260101-120000-A",
                job_json={"finished_at": "2026-06-04T12:00:00+00:00"},
            )
            _make_job_dir(settings, "20260101-130000-B")

            summary = await runner.clean_old_jobs(keep=1)
            logs = settings.codex_task_root / "logs"
            survivors = sorted(p.name for p in logs.iterdir() if p.is_dir())
            if survivors != ["20260101-120000-A"]:
                return CheckResult(
                    name, False,
                    f"expected A (finished_at-winner) to survive, got {survivors}",
                )
            if "Cleaned 1 log dirs" not in summary or "Kept 1 recent jobs" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            return CheckResult(name, True, "metadata finished_at sorted above newer id-time")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- entrypoint ----------------------------------------------------------

async def main() -> int:
    results = []
    for test in (
        _test_no_logs_root,
        _test_empty_logs_dir,
        _test_keep_boundary,
        _test_keep_equals_total,
        _test_keep_exceeds_total,
        _test_keep_zero_raises,
        _test_metadata_finished_at_wins,
    ):
        results.append(await test())
    ok = print_results(results)
    print("clean_old_jobs smoke ok" if ok else "clean_old_jobs smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
