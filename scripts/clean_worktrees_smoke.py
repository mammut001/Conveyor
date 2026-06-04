#!/usr/bin/env python3
"""Smoke tests for CodexRunner.clean_old_worktrees.

The hourly codex-telegram-maintain.timer triggers a GC pass when the
worktree count reaches --clean-threshold (lowered 100 -> 30 in commit
84de8a6). The selection logic itself had no smoke until now: someone
breaks the keep_days / legacy / uncompressed-skip branches and we'd
silently accumulate 100+ worktrees before noticing. These tests pin
the selection down so the next regression is loud.

Scope: the *selection* of which worktrees to remove (and which to
skip for still having MEMORY.md). The actual git worktree-remove call
is stubbed to a plain rmtree + tracker; this smoke doesn't need a real
git workspace and never touches the network.

Run with:  .venv/bin/python scripts/clean_worktrees_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results


# ---- fixtures ------------------------------------------------------------

def _make_settings(tmp: Path) -> Settings:
    """Settings with codex_task_root under tmp; other paths are placeholders.

    codex_workspace_root and codex_memory_root don't matter because
    _remove_worktree is stubbed (no real git) and no archive / snapshot
    path is exercised.
    """
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


def _make_daily_worktree(settings: Settings, day: date) -> Path:
    """Create a day-YYYY-MM-DD worktree dir under codex_task_root/worktrees."""
    name = CodexRunner.DAILY_WORKTREE_PREFIX + day.strftime(CodexRunner.DAILY_WORKTREE_FORMAT)
    wt = settings.codex_task_root / "worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _make_legacy_worktree(settings: Settings, name: str) -> Path:
    """Create a non-day- worktree dir (e.g. legacy wt-<job_id> or stray)."""
    wt = settings.codex_task_root / "worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _stub_remove_worktree(runner: CodexRunner) -> list[Path]:
    """Replace _remove_worktree with a plain rmtree + call tracker.

    Returns the list, so tests can assert which paths were *selected*
    for removal independent of whether rmtree succeeded.
    """
    removed: list[Path] = []

    async def _fake_remove(worktree_path: Path) -> None:
        removed.append(worktree_path)
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    runner._remove_worktree = _fake_remove  # type: ignore[method-assign]
    return removed


def _pin_today(runner: CodexRunner, today: date) -> None:
    """Pin _user_today to a fixed date so the smoke is timezone-independent."""
    runner._user_today = lambda _day=None: today  # type: ignore[method-assign]


# ---- branch tests --------------------------------------------------------

async def _test_no_worktrees_root() -> CheckResult:
    """worktrees_root doesn't exist -> short-circuit 'No worktrees to clean.'"""
    name = "clean: worktrees_root missing -> no-op"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            # Deliberately do NOT mkdir worktrees
            runner = CodexRunner(settings)
            removed = _stub_remove_worktree(runner)
            summary = await runner.clean_old_worktrees(keep_days=7)
            if "No worktrees to clean" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if removed:
                return CheckResult(name, False, f"unexpected removes: {removed}")
            return CheckResult(name, True, "missing root short-circuits with no removes")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_empty_worktrees_dir() -> CheckResult:
    """worktrees_root exists but is empty -> loop runs, returns 'Cleaned 0 ...'.

    Only the *missing* root path short-circuits with 'No worktrees to
    clean.'. An empty dir falls through the iter loop and reports zero
    removes, which is the contract callers (auto_maintain) rely on.
    """
    name = "clean: empty worktrees dir -> no-op"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            (settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
            runner = CodexRunner(settings)
            removed = _stub_remove_worktree(runner)
            summary = await runner.clean_old_worktrees(keep_days=7)
            if "Cleaned 0 legacy" not in summary or "0 old daily worktrees" not in summary:
                return CheckResult(name, False, f"unexpected: {summary!r}")
            if removed:
                return CheckResult(name, False, f"unexpected removes: {removed}")
            return CheckResult(name, True, "empty dir -> 'Cleaned 0 ...' (no removes)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_keeps_within_window() -> CheckResult:
    """10 dailies (today + 9 prior) + keep_days=7 -> keep today + 7 prior, drop 2 oldest."""
    name = "clean: keep_days=7 drops the 2 oldest non-today dailies"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            (settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
            today = date(2026, 6, 4)
            runner = CodexRunner(settings)
            _pin_today(runner, today)
            removed = _stub_remove_worktree(runner)

            # 10 unique days: today, then 9 prior going back
            all_days = [today - timedelta(days=i) for i in range(10)]
            for day in all_days:
                _make_daily_worktree(settings, day)

            summary = await runner.clean_old_worktrees(keep_days=7)
            wt_root = settings.codex_task_root / "worktrees"
            # daily list (after today filter) sorted desc keeps the 7 most recent
            # prior, removes the 2 oldest. Total kept = today + 7 = 8 dailies.
            expected_removed = {
                wt_root / "day-2026-05-26",
                wt_root / "day-2026-05-27",
            }
            actual_removed = set(removed)
            if actual_removed != expected_removed:
                return CheckResult(
                    name, False,
                    f"removed set mismatch.\nexpected={sorted(p.name for p in expected_removed)}\n"
                    f"actual={sorted(p.name for p in actual_removed)}",
                )
            if "2 old daily worktrees" not in summary:
                return CheckResult(name, False, f"unexpected summary: {summary!r}")
            # And today + 7 prior must still be on disk
            for kept_day in (today, *all_days[1:8]):
                wt = wt_root / ("day-" + kept_day.strftime(CodexRunner.DAILY_WORKTREE_FORMAT))
                if not wt.exists():
                    return CheckResult(name, False, f"kept worktree vanished: {wt}")
            return CheckResult(name, True, "kept 8 (today + 7 prior), removed 2 oldest")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_uncompressed_skip() -> CheckResult:
    """An old daily that still has MEMORY.md is skipped (compress is the cure)."""
    name = "clean: old daily with MEMORY.md is skipped, not removed"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            (settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
            today = date(2026, 6, 4)
            runner = CodexRunner(settings)
            _pin_today(runner, today)
            removed = _stub_remove_worktree(runner)

            all_days = [today - timedelta(days=i) for i in range(10)]
            for day in all_days:
                _make_daily_worktree(settings, day)

            # Plant MEMORY.md on 2026-05-26 (oldest = would otherwise be removed)
            uncompressed_wt = settings.codex_task_root / "worktrees" / "day-2026-05-26"
            (uncompressed_wt / CodexRunner.MEMORY_FILENAME).write_text(
                "## preference\n- needs compress first\n", encoding="utf-8"
            )

            summary = await runner.clean_old_worktrees(keep_days=7)
            wt_root = settings.codex_task_root / "worktrees"
            if uncompressed_wt in removed:
                return CheckResult(name, False, "uncompressed was selected for removal")
            if not uncompressed_wt.exists():
                return CheckResult(name, False, "uncompressed dir was removed on disk")
            if "Skipped 1 uncompressed" not in summary:
                return CheckResult(name, False, f"expected skip message, got: {summary!r}")
            # The other old daily (5/27) without MEMORY.md still gets removed
            expected_removed = {wt_root / "day-2026-05-27"}
            actual_removed = set(removed)
            if actual_removed != expected_removed:
                return CheckResult(
                    name, False,
                    f"removed set mismatch.\nexpected={sorted(p.name for p in expected_removed)}\n"
                    f"actual={sorted(p.name for p in actual_removed)}",
                )
            return CheckResult(name, True, "MEMORY.md presence preserves worktree from GC")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_legacy_always_removed() -> CheckResult:
    """Non-day- worktrees (e.g. wt-<job_id>) are always removed, no keep threshold."""
    name = "clean: legacy (non-day-) worktrees removed with no keep threshold"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            (settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
            today = date(2026, 6, 4)
            runner = CodexRunner(settings)
            _pin_today(runner, today)
            removed = _stub_remove_worktree(runner)

            _make_daily_worktree(settings, today)
            _make_legacy_worktree(settings, "wt-abc123")
            _make_legacy_worktree(settings, "wt-def456")
            _make_legacy_worktree(settings, "some-other-dir")

            summary = await runner.clean_old_worktrees(keep_days=7)
            wt_root = settings.codex_task_root / "worktrees"
            expected = {
                wt_root / "wt-abc123",
                wt_root / "wt-def456",
                wt_root / "some-other-dir",
            }
            actual = set(removed)
            if actual != expected:
                return CheckResult(
                    name, False,
                    f"legacy mismatch.\nexpected={sorted(p.name for p in expected)}\n"
                    f"actual={sorted(p.name for p in actual)}",
                )
            if "3 legacy worktrees" not in summary:
                return CheckResult(name, False, f"unexpected summary: {summary!r}")
            # Today must be kept
            today_wt = wt_root / "day-2026-06-04"
            if not today_wt.exists():
                return CheckResult(name, False, "today's worktree was wrongly removed")
            return CheckResult(name, True, "all 3 legacy removed, today kept")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


async def _test_unparseable_day_is_legacy() -> CheckResult:
    """day- prefix with an unparseable date (regex match, strptime fail) -> legacy."""
    name = "clean: day- prefix with unparseable date falls through to legacy"
    try:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = _make_settings(tmp)
            (settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
            today = date(2026, 6, 4)
            runner = CodexRunner(settings)
            _pin_today(runner, today)
            removed = _stub_remove_worktree(runner)

            _make_daily_worktree(settings, today)
            _make_legacy_worktree(settings, "day-2026-13-99")  # regex match, strptime fail
            _make_legacy_worktree(settings, "day-notadate")     # regex fails outright

            summary = await runner.clean_old_worktrees(keep_days=7)
            wt_root = settings.codex_task_root / "worktrees"
            if "2 legacy worktrees" not in summary:
                return CheckResult(name, False, f"unexpected summary: {summary!r}")
            for bad in ("day-2026-13-99", "day-notadate"):
                if (wt_root / bad).exists():
                    return CheckResult(name, False, f"unparseable day survived: {bad}")
            if not (wt_root / "day-2026-06-04").exists():
                return CheckResult(name, False, "today's worktree was wrongly removed")
            return CheckResult(name, True, "both malformed day- dirs went to legacy bucket")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- entrypoint ----------------------------------------------------------

async def main() -> int:
    results = []
    for test in (
        _test_no_worktrees_root,
        _test_empty_worktrees_dir,
        _test_keeps_within_window,
        _test_uncompressed_skip,
        _test_legacy_always_removed,
        _test_unparseable_day_is_legacy,
    ):
        results.append(await test())
    ok = print_results(results)
    print("clean_worktrees smoke ok" if ok else "clean_worktrees smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
