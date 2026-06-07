"""runner/cli.py — split out of runner.py.

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
from runner.core import CodexRunner
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text

import argparse
import sys


def _find_env_file(start: Path | None = None) -> Path | None:
    """Walk up from `start` (or cwd) looking for the first .env file.

    Skips virtualenv / build / cache directories. Used by the CLI subcommands
    when they're not given --env-file explicitly. Returns None if not found.
    """
    cur = (start or Path.cwd()).resolve()
    while True:
        env = cur / ".env"
        if env.is_file():
            return env
        if cur.parent == cur:
            return None
        if cur.name in MEMO_ENV_SKIP_DIRS:
            return None
        cur = cur.parent


def _cli_load_runner(env_file: Path) -> CodexRunner:
    """Load settings from .env and construct a CodexRunner. Raises on missing env."""
    settings = load_settings(str(env_file))  # raises RuntimeError if required env missing
    return CodexRunner(settings)


async def _classify_and_append(runner: CodexRunner, content: str) -> str:
    category = await runner.classify_memo(content)
    auto_ts = category == "fact"
    return await runner.append_memo(category, content, auto_timestamp=auto_ts)


def _cli_memorize(args: argparse.Namespace) -> int:
    """memorize subcommand: append a categorized entry to today's MEMORY.md."""
    env_file = Path(args.env_file) if args.env_file else _find_env_file()
    if env_file is None:
        print("error: could not locate .env (pass --env-file)", file=sys.stderr)
        return 1
    try:
        runner = _cli_load_runner(env_file)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.category:
            result = asyncio.run(
                runner.append_memo(
                    args.category,
                    args.content,
                    auto_timestamp=(args.category == "fact"),
                )
            )
        else:
            # No --category: classify first, then append.
            result = asyncio.run(_classify_and_append(runner, args.content))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(result)
    return 0


def _cli_recall_memory(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file) if args.env_file else _find_env_file()
    if env_file is None:
        print("error: could not locate .env (pass --env-file)", file=sys.stderr)
        return 1
    try:
        runner = _cli_load_runner(env_file)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(runner.read_memory(args.category), end="")
    return 0


def _cli_recall_journal(args: argparse.Namespace) -> int:
    env_file = Path(args.env_file) if args.env_file else _find_env_file()
    if env_file is None:
        print("error: could not locate .env (pass --env-file)", file=sys.stderr)
        return 1
    try:
        runner = _cli_load_runner(env_file)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(runner.read_journal(args.date, args.category), end="")
    return 0


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m runner",
        description="telegram-codex-runner CLI (memorize / recall-memory / recall-journal)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_mem = sub.add_parser("memorize", help="append a categorized entry to today's MEMORY.md")
    p_mem.add_argument("content", help="the content to memorize")
    p_mem.add_argument(
        "--category",
        choices=("preference", "fact", "tool-quirk", "convention", "unfiled"),
        help="explicit category; default = let runner.classify_memo pick",
    )
    p_mem.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the '记下了: ...' confirmation line",
    )
    p_mem.add_argument("--env-file", help="path to .env (default: walk up from cwd)")
    p_mem.set_defaults(func=_cli_memorize)

    p_rm = sub.add_parser("recall-memory", help="read today's MEMORY.md (or one category)")
    p_rm.add_argument(
        "category",
        nargs="?",
        default=None,
        choices=("preference", "fact", "tool-quirk", "convention", "unfiled"),
        help="optional category; default = full file",
    )
    p_rm.add_argument("--env-file", help="path to .env")
    p_rm.set_defaults(func=_cli_recall_memory)

    p_rj = sub.add_parser("recall-journal", help="read a past day's archived journal")
    p_rj.add_argument("date", help="YYYY-MM-DD")
    p_rj.add_argument(
        "category",
        nargs="?",
        default=None,
        choices=("preference", "fact", "tool-quirk", "convention", "unfiled"),
        help="optional category; default = full archive",
    )
    p_rj.add_argument("--env-file", help="path to .env")
    p_rj.set_defaults(func=_cli_recall_journal)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    return args.func(args)
