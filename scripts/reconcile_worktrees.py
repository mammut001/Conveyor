#!/usr/bin/env python3
"""CLI utility to sweep orphan worktrees."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile orphan worktrees.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--dry-run", action="store_true", help="Scan and list orphans without deleting")
    parser.add_argument("--ttl-hours", type=float, default=24.0, help="TTL in hours (default: 24)")
    args = parser.parse_args()

    settings = load_settings(args.env)
    runner = CodexRunner(settings)
    ttl_seconds = int(args.ttl_hours * 3600)
    result = asyncio.run(runner.reconcile_orphans(dry_run=args.dry_run, ttl_seconds=ttl_seconds))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
