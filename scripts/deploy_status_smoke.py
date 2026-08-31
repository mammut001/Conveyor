#!/usr/bin/env python3
"""Behavior/static smoke for /deploy_status and canonical deployment metadata."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLE_STATUS = {
    "deployed_at": "2026-06-11T03:00:00Z",
    "source": "github-actions",
    "git_sha": "abc1234",
    "git_ref": "main",
    "run_id": "12345",
    "remote_dir": "/opt/conveyor",
    "smoke": "passed",
    "backup_path": "/opt/conveyor/.deploy-backups/example",
    "database_path": "/home/ubuntu/.codex/state/job_queue.sqlite3",
    "services": {"telegram": "active", "feishu": "active", "web": "active"},
    "rollback_attempted": False,
    "previous_commit": "def5678",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"deploy status smoke failed: {message}")


async def run_handler(cwd: Path) -> str:
    from handlers.commands import _deploy_status
    msg = MagicMock()
    port = MagicMock()
    port.reply = AsyncMock()
    runner = MagicMock()
    settings = SimpleNamespace(conveyor_progress_mode="compact")
    old = Path.cwd()
    try:
        os.chdir(cwd)
        await _deploy_status(msg, port, runner, settings, "")
    finally:
        os.chdir(old)
    port.reply.assert_awaited_once()
    return str(port.reply.call_args[0][1])


async def behavior_checks() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ".deploy-status.json").write_text(json.dumps(SAMPLE_STATUS), encoding="utf-8")
        output = await run_handler(root)
        require("2026-06-11" in output, "valid status missing deployment time")
        require("abc1234" in output, "valid status missing git SHA")
        require("passed" in output, "valid status missing smoke result")
        require("active" in output, "valid status missing service state")
        require("compact" in output, "valid status missing progress mode")

    with tempfile.TemporaryDirectory() as temporary:
        output = await run_handler(Path(temporary))
        require("暂无部署状态记录" in output, "missing status file did not degrade gracefully")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ".deploy-status.json").write_text("{bad json!!", encoding="utf-8")
        output = await run_handler(root)
        require("读取失败" in output or "暂无" in output or "Deploy" in output,
                "invalid status JSON was not handled")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        secret_status = dict(SAMPLE_STATUS)
        secret_status["secret_note"] = "TELEGRAM_BOT_TOKEN=123:ABC"
        secret_status["env_content"] = "API_KEY=sk-very-secret"
        (root / ".deploy-status.json").write_text(json.dumps(secret_status), encoding="utf-8")
        output = await run_handler(root)
        require("123:ABC" not in output and "sk-very-secret" not in output,
                "deploy status leaked unknown secret fields")


def static_checks() -> None:
    from handlers.commands import COMMAND_TABLE
    require(COMMAND_TABLE.get("deploy_status") is not None, "/deploy_status not registered")

    remote = (ROOT / "scripts" / "deploy_vps.sh").read_text(encoding="utf-8")
    manual = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    for field in ("deployed_at", "source", "git_sha", "smoke", "services",
                  "backup_path", "database_path", "previous_commit"):
        require(f'"{field}"' in remote, f"canonical deploy status missing field {field}")
    require('git show "${TARGET_COMMIT}:scripts/deploy_vps.sh"' in manual,
            "manual deploy is not delegated to canonical deployer")
    require(".deploy-status.json" not in "\n".join(
        line for line in manual.splitlines() if not line.lstrip().startswith("#")
    ), "manual wrapper still writes an independent status file")


def main() -> int:
    static_checks()
    asyncio.run(behavior_checks())
    print("deploy status smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
