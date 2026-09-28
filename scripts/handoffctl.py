#!/usr/bin/env python3
"""Operator CLI for Conveyor human takeover coordination.

This command manages the secret-free takeover lease only. The graphical
transport (for example loopback noVNC) is started separately so Conveyor never
needs to own or log remote-desktop credentials.
"""
from __future__ import annotations

import argparse
import json
import sys

from config import load_settings
from human_takeover import ALLOWED_REASONS, HumanTakeoverStore


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Conveyor human takeover sessions")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="open an exclusive human GUI lease")
    start.add_argument("--reason", choices=ALLOWED_REASONS, required=True)
    start.add_argument("--task-id", default="")
    start.add_argument("--requested-by", default="operator")
    start.add_argument("--ttl", type=int, default=300, help="30-1800 seconds")

    for name in ("activate", "complete", "cancel"):
        command = sub.add_parser(name)
        command.add_argument("session_id")
    sub.add_parser("status")

    args = parser.parse_args()
    settings = load_settings()
    store = HumanTakeoverStore(settings)

    try:
        if args.command == "start":
            result = store.start(
                reason=args.reason,
                task_id=args.task_id or None,
                requested_by=args.requested_by or None,
                ttl_seconds=args.ttl,
            )
        elif args.command == "activate":
            result = store.activate(args.session_id)
        elif args.command == "complete":
            result = store.complete(args.session_id)
        elif args.command == "cancel":
            result = store.cancel(args.session_id)
        else:
            result = store.current()
    except (ValueError, RuntimeError) as exc:
        _print({"ok": False, "error": str(exc)})
        return 2

    if args.command in {"activate", "complete", "cancel"} and result is None:
        _print({"ok": False, "error": "takeover session not found or invalid state"})
        return 1

    _print({"ok": True, "takeover": HumanTakeoverStore.public(result)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
