#!/usr/bin/env python3
"""Smoke tests for scripts/compress_day.compress_if_needed + _archive_day.

The 12:00 archive path is the single most important "memory" feature
in the runner: it's how MEMORY.md content lands in ~/.codex/JOURNAL/ and
gets snapshotted, so the user's 2026-06-03 worktree doesn't bloat forever.
The path has 5 branches in compress_if_needed and a real side-effecting
_archive_day that writes the journal file, renames MEMORY.md, and snapshots
the worktree. Both the hour-gate logic and the archive side-effects have
historically been silent regressions (e.g. compress_if_needed being called
without await in auto_maintain), so this smoke walks all 5 branches and an
AST guard for the await on _archive_day.

Tests use a tmp dir for codex_task_root and codex_memory_root, never the
real ~/.codex/ or /srv/codex-telegram-runner/worktrees/. classify_memo
(network) is bypassed by making the test MEMORY.md contain NO "## unfiled"
section, which makes reclassify_unfiled return early.

Run with:  .venv/bin/python scripts/compress_day_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from scripts import compress_day as _compress_day_mod
from scripts.compress_day import compress_if_needed
from scripts.harness_common import CheckResult, print_results
from runner import CodexRunner


COMPRESS_DAY_PY = Path(__file__).resolve().parents[1] / "scripts" / "compress_day.py"

# Async callees that compress_if_needed should always await. Kept explicit
# (not auto-detected from inspect) so the test stays robust against
# renames / moves of the callees.
ASYNC_NAME_CALLEES = {"_archive_day"}


# ---- fixtures ------------------------------------------------------------

def _make_settings(tmp: Path) -> Settings:
    """Fake Settings with codex_task_root / codex_memory_root under tmp.

    Only the fields compress_day actually touches matter; the rest are
    placeholders so the @dataclass(frozen=True) constructor is happy.
    """
    task_root = tmp / "task"
    memory_root = tmp / "memory"
    (task_root / "worktrees").mkdir(parents=True, exist_ok=True)
    (memory_root / "state").mkdir(parents=True, exist_ok=True)
    (memory_root / "JOURNAL").mkdir(parents=True, exist_ok=True)
    (memory_root / "snapshots").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="fake",  # unused by compress_day
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp / "ws",
        codex_bin="codex",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=20,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=memory_root,
        user_timezone="UTC",
    )


def _make_daily_worktree(settings: Settings, day: datetime) -> Path:
    """Create a day-YYYY-MM-DD worktree dir under codex_task_root/worktrees."""
    wt = settings.codex_task_root / "worktrees" / (
        CodexRunner.DAILY_WORKTREE_PREFIX + day.strftime(CodexRunner.DAILY_WORKTREE_FORMAT)
    )
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _write_memory(worktree: Path, content: str) -> Path:
    path = worktree / CodexRunner.MEMORY_FILENAME
    path.write_text(content, encoding="utf-8")
    return path


def _write_state(settings: Settings, day: datetime) -> None:
    state_file = settings.codex_memory_root / "state" / "last_compress_date.txt"
    state_file.write_text(day.strftime("%Y-%m-%d") + "\n", encoding="utf-8")


def _read_state(settings: Settings) -> str | None:
    state_file = settings.codex_memory_root / "state" / "last_compress_date.txt"
    if not state_file.exists():
        return None
    return state_file.read_text(encoding="utf-8").strip() or None


from contextlib import contextmanager


@contextmanager
def _frozen_clock(fixed_now: datetime):
    """Pin scripts.compress_day._user_now to a deterministic value.

    compress_if_needed derives its notion of "today" and the current hour
    from _user_now(settings), which uses settings.user_timezone and the
    real wall clock. We don't want the smoke to depend on what UTC hour
    the test runner happens to be at, so we swap the function out for
    the duration of the with-block and restore on exit.
    """
    original = _compress_day_mod._user_now
    _compress_day_mod._user_now = lambda _settings: fixed_now
    try:
        yield
    finally:
        _compress_day_mod._user_now = original


# ---- branch tests --------------------------------------------------------

async def _test_already_ran_today() -> CheckResult:
    """Branch 1: last.date() >= today.date() -> skip."""
    name = "compress: already-ran-today branch returns skip message"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            today = datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo("UTC"))
            _make_daily_worktree(settings, today)
            _make_daily_worktree(settings, datetime(2026, 6, 3, 12, 0))
            _write_state(settings, today)

            summary = await compress_if_needed(settings)
            if "already ran today" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if _read_state(settings) != today.strftime("%Y-%m-%d"):
                return CheckResult(name, False, f"state mutated: {_read_state(settings)!r}")
            return CheckResult(name, True, "skip path verified, state untouched")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_no_prior_day() -> CheckResult:
    """Branch 2: candidate is None -> write state to today, no archive."""
    name = "compress: no-prior-day branch writes state and skips"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            today = datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo("UTC"))
            _make_daily_worktree(settings, today)

            summary = await compress_if_needed(settings)
            if "no prior day worktree" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if _read_state(settings) != today.strftime("%Y-%m-%d"):
                return CheckResult(name, False, f"state not written: {_read_state(settings)!r}")
            journal_dir = settings.codex_memory_root / "JOURNAL"
            if any(journal_dir.iterdir()):
                return CheckResult(name, False, f"unexpected journal entry: {list(journal_dir.iterdir())}")
            return CheckResult(name, True, "state written to today, no journal created")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_last_covers_candidate() -> CheckResult:
    """Branch 3: last >= candidate (but not today) -> mark today, no double-archive."""
    name = "compress: last-covers-candidate branch marks today, no double-archive"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            today = datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo("UTC"))
            prior = datetime(2026, 6, 3, 12, 0)
            _make_daily_worktree(settings, today)
            prior_wt = _make_daily_worktree(settings, prior)
            _write_memory(prior_wt, "## preference\n- exists but should NOT be archived again\n")
            _write_state(settings, prior)

            summary = await compress_if_needed(settings)
            if "already covers candidate" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if _read_state(settings) != today.strftime("%Y-%m-%d"):
                return CheckResult(name, False, f"state not advanced: {_read_state(settings)!r}")
            journal_dir = settings.codex_memory_root / "JOURNAL"
            if any(journal_dir.iterdir()):
                return CheckResult(name, False, f"unexpected journal entry: {list(journal_dir.iterdir())}")
            if not (prior_wt / CodexRunner.MEMORY_FILENAME).exists():
                return CheckResult(name, False, "prior MEMORY.md was wrongly renamed")
            return CheckResult(name, True, "no double-archive, MEMORY.md preserved, state advanced")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_hour_gate() -> CheckResult:
    """Branch 4: not force and now.hour < 12 -> wait. force=True bypasses."""
    name = "compress: hour gate (now.hour<12, no force) returns waiting, force=True archives"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            # Pin "now" to 09:00 UTC on the candidate's day so the hour
            # gate fires deterministically regardless of wall clock.
            today = datetime(2026, 6, 4, 9, 0, tzinfo=ZoneInfo("UTC"))
            prior = datetime(2026, 6, 3, 18, 0)
            _make_daily_worktree(settings, today)
            prior_wt = _make_daily_worktree(settings, prior)
            _write_memory(prior_wt, "## preference\n- should NOT archive at 9am\n")
            _write_state(settings, datetime(2026, 6, 2, 12, 0))

            with _frozen_clock(today):
                summary = await compress_if_needed(settings, force=False)
                if "waiting" not in summary:
                    return CheckResult(name, False, f"unexpected: {summary!r}")
                summary_forced = await compress_if_needed(settings, force=True)
                if "archived" not in summary_forced:
                    return CheckResult(name, False, f"force did not archive: {summary_forced!r}")
            return CheckResult(name, True, "9am returns waiting, force=True archives")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_full_archive() -> CheckResult:
    """Branch 5: force=True past the gate -> real _archive_day side effects."""
    name = "compress: full archive writes journal, renames MEMORY.md, snapshots, advances state"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            today = datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo("UTC"))
            prior = datetime(2026, 6, 3, 12, 0)
            _make_daily_worktree(settings, today)
            prior_wt = _make_daily_worktree(settings, prior)
            # No "## unfiled" section -> reclassify_unfiled returns early
            # without hitting the network.
            memory_content = (
                "## preference\n- user prefers dark mode\n\n"
                "## fact\n- sample fact line\n"
            )
            _write_memory(prior_wt, memory_content)
            _write_state(settings, datetime(2026, 6, 2, 12, 0))

            with _frozen_clock(today):
                summary = await compress_if_needed(settings, force=True)
                if "archived 2026-06-03" not in summary:
                    return CheckResult(name, False, f"unexpected: {summary!r}")

            day_stamp = "2026-06-03"
            journal_file = settings.codex_memory_root / "JOURNAL" / f"{day_stamp}.md"
            if not journal_file.exists():
                return CheckResult(name, False, f"journal not written at {journal_file}")
            journal = journal_file.read_text(encoding="utf-8")
            if f"# MEMORY.md archive \u2014 {day_stamp}" not in journal:
                return CheckResult(name, False, "journal header missing day stamp")
            if "user prefers dark mode" not in journal:
                return CheckResult(name, False, "journal missing archived body content")
            if "- reclassified: 0 unfiled" in journal:
                return CheckResult(name, False, "reclassify line should be absent when count=0")

            archived_path = prior_wt / f"{CodexRunner.MEMORY_FILENAME}.archived-{day_stamp}"
            if (prior_wt / CodexRunner.MEMORY_FILENAME).exists():
                return CheckResult(name, False, "MEMORY.md was not renamed")
            if not archived_path.exists():
                return CheckResult(name, False, f"archived MEMORY.md missing at {archived_path}")

            snapshots = list((settings.codex_memory_root / "snapshots").iterdir())
            matching = [s for s in snapshots if s.name.startswith(f"{day_stamp}-")]
            if not matching:
                return CheckResult(name, False, f"no snapshot for {day_stamp}: {snapshots}")

            if _read_state(settings) != today.strftime("%Y-%m-%d"):
                return CheckResult(name, False, f"state not advanced: {_read_state(settings)!r}")

            with _frozen_clock(today):
                summary_again = await compress_if_needed(settings, force=True)
                if "already ran today" not in summary_again:
                    return CheckResult(name, False, f"idempotency broken: {summary_again!r}")
            return CheckResult(name, True, "archive + rename + snapshot + state all good, idempotent")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- AST regression guard ------------------------------------------------

def _function_def(tree, name):
    return next(
        (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
        None,
    )


def _calls_with_await_status(func_def):
    """Yield (Call node, 'awaited'|'plain') for every Call in func_def."""
    awaited_call_ids = {
        id(node.value)
        for node in ast.walk(func_def)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call):
            yield node, "awaited" if id(node) in awaited_call_ids else "plain"


def _callee_id(call):
    if isinstance(call.func, ast.Name):
        return ("name", call.func.id)
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return ("attr", f"{call.func.value.id}.{call.func.attr}")
    return None


def _test_await_guard() -> CheckResult:
    """_archive_day is async; compress_if_needed must await it.

    Same shape of regression as the auto_maintain await bug: a coroutine
    created and not awaited makes the timer log TypeErrors. Catching it
    at code-level is cheap.
    """
    name = "compress: _archive_day is awaited in compress_if_needed"
    try:
        tree = ast.parse(COMPRESS_DAY_PY.read_text(encoding="utf-8"))
        func_def = _function_def(tree, "compress_if_needed")
        if func_def is None:
            return CheckResult(name, False, "compress_if_needed not found")
        un_awaited = []
        for call, status in _calls_with_await_status(func_def):
            target = _callee_id(call)
            if target is None or target[0] != "name":
                continue
            if target[1] in ASYNC_NAME_CALLEES and status == "plain":
                un_awaited.append(target[1])
        ok = not un_awaited
        detail = "all awaited" if ok else f"un-awaited: {sorted(un_awaited)}"
        return CheckResult(name, ok, detail)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- entrypoint ----------------------------------------------------------

async def main() -> int:
    results = []
    for test in (
        _test_already_ran_today,
        _test_no_prior_day,
        _test_last_covers_candidate,
        _test_hour_gate,
        _test_full_archive,
    ):
        results.append(await test())
    results.append(_test_await_guard())
    ok = print_results(results)
    print("compress_day smoke ok" if ok else "compress_day smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
