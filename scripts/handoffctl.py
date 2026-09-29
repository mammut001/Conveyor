#!/usr/bin/env python3
"""Operator CLI for Conveyor human takeover coordination.

This command manages the secret-free takeover lease only. The graphical
transport (for example loopback noVNC) is started separately so Conveyor never
needs to own or log remote-desktop credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from config import load_settings
from desktop_computer_requests import cancel_pending_computer_steps, has_claimed_computer_steps
from desktop_observe_requests import cancel_pending_observe_requests, has_claimed_observe_requests
from human_takeover import ALLOWED_REASONS, HumanTakeoverStore


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _transport_state_dir() -> Path:
    runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not runtime_base.is_dir() or not os.access(runtime_base, os.W_OK):
        runtime_base = Path(f"/tmp/conveyor-handoff-{os.getuid()}")
    return Path(os.environ.get("CONVEYOR_HANDOFF_STATE_DIR") or runtime_base / "conveyor-handoff")


def _transport_running() -> bool:
    """Fail closed if a VNC/noVNC process still has a live pid file."""
    # A Serve route survives in the daemon if its CLI exits unexpectedly.
    # The marker is removed only after stop verified the route is absent.
    if (_transport_state_dir() / "tailscale-serve.port").exists():
        return True
    for name in ("x11vnc.pid", "websockify.pid"):
        try:
            raw_pid = (_transport_state_dir() / name).read_text(encoding="ascii").strip()
            pid = int(raw_pid)
            if pid <= 0:
                continue
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (FileNotFoundError, ProcessLookupError, ValueError):
            continue
        except OSError:
            continue
        return True
    return False


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
            # Stop new node claims first, discard queued pre-handoff plans,
            # then wait for an already claimed action/screenshot to finish
            # before the operator can open the remote desktop.
            cancel_pending_computer_steps(settings)
            cancel_pending_observe_requests(settings)
            deadline = time.monotonic() + max(30, int(settings.conveyor_computer_max_seconds) + 5)
            while has_claimed_computer_steps(settings) or has_claimed_observe_requests(settings):
                current = store.current()
                if current is None or current.get("id") != result.get("id"):
                    raise RuntimeError("takeover expired before the desktop became idle; no remote desktop was started")
                if time.monotonic() >= deadline:
                    raise RuntimeError("a computer-use action is still in flight; takeover remains open and the remote desktop must stay closed")
                time.sleep(0.1)
            current = store.current()
            if current is None or current.get("id") != result.get("id"):
                raise RuntimeError("takeover expired before the desktop became idle; no remote desktop was started")
            result = current
        elif args.command == "activate":
            result = store.activate(args.session_id)
        elif args.command == "complete":
            if _transport_running():
                raise RuntimeError("stop the VNC/noVNC transport before completing takeover so the Agent cannot resume while the operator still owns the desktop")
            result = store.complete(args.session_id)
        elif args.command == "cancel":
            if _transport_running():
                raise RuntimeError("stop the VNC/noVNC transport before cancelling takeover so the Agent cannot resume while the operator still owns the desktop")
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
