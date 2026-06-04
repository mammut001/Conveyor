#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner


async def run_command(args: argparse.Namespace) -> str:
    settings = load_settings(args.env)
    runner = CodexRunner(settings)
    if args.command == "jobs":
        return runner.jobs_text(args.limit)
    if args.command == "last":
        return runner.last_text()
    if args.command == "diff":
        return await runner.diff_text()
    if args.command == "clean":
        return await runner.clean_old_jobs(args.keep)
    if args.command == "discard":
        return await runner.discard_last_job()
    if args.command == "apply":
        return await runner.apply_last_job()
    raise RuntimeError(f"unknown command: {args.command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Telegram Codex runner job lifecycle from the VPS.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    jobs = subparsers.add_parser("jobs", help="Show recent jobs")
    jobs.add_argument("--limit", type=int, default=8)
    subparsers.add_parser("last", help="Show latest final result")
    subparsers.add_parser("diff", help="Show latest job diff")
    clean = subparsers.add_parser("clean", help="Remove old logs/worktrees")
    clean.add_argument("--keep", type=int, default=20)
    subparsers.add_parser("discard", help="Remove latest job worktree")
    subparsers.add_parser("apply", help="Apply latest job changes to main workspace")

    args = parser.parse_args()
    print(asyncio.run(run_command(args)))


if __name__ == "__main__":
    main()
