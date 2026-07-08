#!/usr/bin/env python3
"""Read-only real Cua driver smoke for the Mac desktop agent.

This is intentionally not part of ``make smoke`` because it talks to the
operator's real macOS desktop through the local ``cua-driver`` binary.
It performs no click/type/hotkey actions. It checks:

  1. driver availability/version/permissions metadata
  2. tool list includes the current desktop-state surface
  3. Conveyor's LocalCuaTransport can run observe and return metadata only

Run on the Mac agent after installing Cua and granting permissions:

    python3 scripts/cua_driver_real_smoke.py --cmd "cua-driver mcp"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config import Settings  # noqa: E402
from desktop_cua import _driver_binary, build_driver, probe_cua_driver  # noqa: E402


def _settings(cmd: str) -> Settings:
    root = Path(tempfile.mkdtemp(prefix="conv_cua_real_smoke_"))
    return Settings(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "task",
        codex_model=None,
        codex_timeout_seconds=3600,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=3,
        codex_memory_root=root,
        user_timezone="UTC",
        conveyor_cua_driver_cmd=cmd,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only real Cua driver smoke")
    parser.add_argument("--cmd", default="cua-driver mcp", help="Configured Cua command")
    args = parser.parse_args()

    settings = _settings(args.cmd)
    probe = probe_cua_driver(args.cmd, settings=settings)
    result: dict = {"probe": probe}
    if not probe.get("available"):
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 2

    binary = _driver_binary(args.cmd)
    tools = subprocess.run(
        [binary, "list-tools"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    result["list_tools_ok"] = tools.returncode == 0
    result["has_get_desktop_state"] = "get_desktop_state" in (tools.stdout or "")
    result["has_click"] = "click" in (tools.stdout or "")

    driver = build_driver(settings, fake=False, node_id="mac-real-smoke")
    observe = driver.execute({"action": "observe"})
    result["observe"] = observe
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if observe.get("result_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
