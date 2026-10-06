#!/usr/bin/env python3
"""Print `export` lines for the live desktop session of the current user.

The services that draw on, or click in, the host desktop start outside it,
so they borrow DISPLAY and the session bus from the session process itself.

The process is matched by its name and must carry a DISPLAY. Matching the
command line instead picks up anything that merely mentions the session —
an ssh command, a grep — and hands the caller an environment with no display.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

SESSION_PROCESS_NAMES = ("xfce4-session",)
WANTED = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XAUTHORITY")


def session_env(proc: Path = Path("/proc"), uid: int | None = None) -> dict[str, str]:
    uid = os.getuid() if uid is None else uid
    for entry in sorted(proc.iterdir(), key=lambda p: p.name):
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            if (entry / "comm").read_text().strip() not in SESSION_PROCESS_NAMES:
                continue
            raw = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        found: dict[str, str] = {}
        for item in raw:
            key, sep, value = item.partition(b"=")
            name = key.decode("ascii", "replace")
            if sep and name in WANTED:
                found[name] = value.decode("utf-8", "replace")
        if found.get("DISPLAY"):
            return found
    return {}


def main() -> int:
    found = session_env()
    if not found:
        print("no desktop session with a display", file=sys.stderr)
        return 1
    found.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    if "XDG_RUNTIME_DIR" not in found and runtime_dir.is_dir():
        found["XDG_RUNTIME_DIR"] = str(runtime_dir)
    for name, value in found.items():
        print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
