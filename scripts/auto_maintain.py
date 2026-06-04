#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner
from scripts.backfill_metadata import backfill_job_metadata
from scripts.compress_day import compress_if_needed
from scripts.doctor import check_disk, check_latest_job, check_runtime_dirs, check_workspace
from scripts.harness_common import check_minimax_models, check_systemd_active
from scripts.health_snapshot import health_snapshot, write_snapshot
from scripts.offline_harnesses import run_offline_harnesses
from scripts.security_audit import run_security_audit
from scripts.telegram_api import send_message
from scripts.triage import triage_lines


@dataclass(frozen=True)
class MaintenanceOutcome:
    code: int
    summary: str


def _count_from_detail(detail: str) -> int:
    match = re.search(r"(\d+)", detail)
    return int(match.group(1)) if match else 0


async def run_maintenance(env_file: str, service_name: str, clean_threshold: int, keep: int) -> MaintenanceOutcome:
    settings = load_settings(env_file)
    results = [
        check_systemd_active(service_name),
        check_workspace(settings),
        check_minimax_models(settings),
        check_disk(settings.codex_task_root),
    ]
    results.extend(check_runtime_dirs(settings))
    results.extend(check_latest_job(settings))
    offline_results = run_offline_harnesses(env_file, include_command=True)
    results.extend(offline_results)

    log_count = 0
    worktree_count = 0
    for result in results:
        if result.name == "log count":
            log_count = _count_from_detail(result.detail)
        elif result.name == "worktree count":
            worktree_count = _count_from_detail(result.detail)

    actions: list[str] = []
    actions.append(await compress_if_needed(settings))
    backfilled = backfill_job_metadata(settings, force=False)
    actions.append(f"Backfilled {backfilled} missing job metadata files.")
    security_results = run_security_audit(env_file, service_name, "1 hour ago")
    fast_snapshot = health_snapshot(env_file, service_name, "1 hour ago", metrics_limit=20, include_security=False, include_offline=False)
    full_snapshot = health_snapshot(
        env_file,
        service_name,
        "1 hour ago",
        metrics_limit=20,
        include_security=True,
        include_offline=True,
        offline_results=offline_results,
        security_results=security_results,
    )
    fast_path = write_snapshot(settings, fast_snapshot, "latest-fast.json")
    full_path = write_snapshot(settings, full_snapshot, "latest-full.json")
    actions.append(f"Wrote health snapshots: {fast_path.name}, {full_path.name}.")

    runner = CodexRunner(settings)
    if log_count >= clean_threshold or worktree_count >= clean_threshold:
        actions.append(await runner.clean_old_jobs(keep))
        actions.append(await runner.clean_old_worktrees(keep_days=7))
    else:
        actions.append(f"No cleanup needed. logs={log_count} worktrees={worktree_count}")

    failed = [result for result in results if not result.ok]
    advice = triage_lines(failed)
    summary = "\n".join(
        [
            "Auto-maintain complete.",
            *actions,
            f"checks={'ok' if not failed else 'failed'}",
            *[result.line() for result in failed],
            *(["Suggested next steps:", *advice] if advice else []),
        ]
    )
    return MaintenanceOutcome(0 if not failed else 1, summary)


async def maintain(env_file: str, service_name: str, clean_threshold: int, keep: int, notify: bool) -> int:
    settings = load_settings(env_file)
    outcome = await run_maintenance(env_file, service_name, clean_threshold, keep)
    print(outcome.summary)
    if notify:
        await asyncio.to_thread(send_message, settings, outcome.summary)
    return outcome.code


def main() -> None:
    parser = argparse.ArgumentParser(description="Run safe autonomous maintenance for the Telegram Codex runner.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="codex-telegram-bot")
    parser.add_argument("--clean-threshold", type=int, default=100)
    parser.add_argument("--keep", type=int, default=50)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(maintain(args.env, args.service, args.clean_threshold, args.keep, args.notify)))


if __name__ == "__main__":
    main()
