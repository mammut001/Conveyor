#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results
from scripts.job_metadata import job_sort_time


def _age_seconds(path: Path) -> int:
    return int((datetime.now(timezone.utc) - job_sort_time(path)).total_seconds())


def _fmt_age(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 72:
        return f"{hours}h"
    return f"{hours // 24}d"


def run_job_audit(env_file: str, stale_minutes: int, sample_limit: int = 5) -> list[CheckResult]:
    settings = load_settings(env_file)
    runner = CodexRunner(settings)
    records = runner.job_records(10000)
    logs_root = settings.codex_task_root / "logs"
    worktrees_root = settings.codex_task_root / "worktrees"
    log_ids = {path.name for path in logs_root.iterdir() if path.is_dir()} if logs_root.exists() else set()
    worktree_ids = {path.name for path in worktrees_root.iterdir() if path.is_dir()} if worktrees_root.exists() else set()

    stale_seconds = stale_minutes * 60
    stale_running = [
        record
        for record in records
        if record.state == "running" and _age_seconds(record.log_dir) >= stale_seconds
    ]
    orphan_worktrees = sorted(worktree_ids - log_ids)
    missing_worktrees = sorted(log_ids - worktree_ids)
    failed = [record for record in records if record.state == "failed"]

    results = [
        CheckResult("job records", True, f"{len(records)} logs={len(log_ids)} worktrees={len(worktree_ids)}"),
        CheckResult(
            "stale running jobs",
            not stale_running,
            "none" if not stale_running else ", ".join(f"{record.id} age={_fmt_age(_age_seconds(record.log_dir))}" for record in stale_running[:sample_limit]),
        ),
        CheckResult(
            "orphan worktrees",
            not orphan_worktrees,
            "none" if not orphan_worktrees else ", ".join(orphan_worktrees[:sample_limit]),
        ),
        CheckResult(
            "logs without worktrees",
            True,
            "none" if not missing_worktrees else f"{len(missing_worktrees)} records; latest samples: {', '.join(missing_worktrees[-sample_limit:])}",
        ),
        CheckResult(
            "failed jobs",
            True,
            "none" if not failed else f"{len(failed)} failed; latest samples: {', '.join(record.id for record in failed[:sample_limit])}",
        ),
    ]
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Codex runner job logs and worktrees for stale or orphaned state.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--stale-minutes", type=int, default=90)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = run_job_audit(args.env, max(1, args.stale_minutes))
    if args.json:
        print(
            json.dumps(
                {
                    "ok": all(result.ok for result in results),
                    "checks": [
                        {"name": result.name, "ok": result.ok, "detail": result.detail}
                        for result in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(0 if all(result.ok for result in results) else 1)
    ok = print_results(results)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
