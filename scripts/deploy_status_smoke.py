#!/usr/bin/env python3
"""deploy_status_smoke.py — tests for /deploy_status command and deploy metadata.

Verifies:
  - /deploy_status handler exists and is callable
  - Valid .deploy-status.json is parsed and displayed
  - Missing .deploy-status.json returns "暂无部署状态记录"
  - Invalid JSON does not crash
  - No .env contents are leaked
  - Output includes deploy time, git sha, smoke result, services, progress mode
  - deploy.sh and deploy_vps.sh write valid JSON structure

Run: .venv/bin/python scripts/deploy_status_smoke.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402


# ---- Helpers ---------------------------------------------------------------

SAMPLE_STATUS = {
    "deployed_at": "2026-06-11T03:00:00Z",
    "source": "github-actions",
    "git_sha": "abc1234",
    "git_ref": "main",
    "run_id": "12345",
    "remote_dir": "/opt/conveyor",
    "smoke": "passed",
    "services": {
        "telegram": "active",
        "feishu": "active",
    },
}


async def _run_deploy_status(status_json: dict | None, cwd: Path) -> str:
    """Run the _deploy_status handler in a controlled cwd and capture output."""
    # Temporarily write .deploy-status.json if provided
    status_file = cwd / ".deploy-status.json"
    wrote = False
    if status_json is not None:
        status_file.write_text(json.dumps(status_json), encoding="utf-8")
        wrote = True

    # Import handler
    from handlers.commands import _deploy_status

    msg = MagicMock()
    port = MagicMock()
    port.reply = AsyncMock()
    runner = MagicMock()
    settings = SimpleNamespace(conveyor_progress_mode="compact")

    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(cwd)
        await _deploy_status(msg, port, runner, settings, "")
    finally:
        os.chdir(old_cwd)
        if wrote and status_file.exists():
            status_file.unlink()

    port.reply.assert_awaited_once()
    return port.reply.call_args[0][1]


# ---- Tests -----------------------------------------------------------------

def _test_handler_registered() -> CheckResult:
    name = "deploy_status: handler registered in COMMAND_TABLE"
    try:
        from handlers.commands import COMMAND_TABLE
        spec = COMMAND_TABLE.get("deploy_status")
        ok = spec is not None
        return CheckResult(name, ok, f"spec={spec}" if ok else "not found in COMMAND_TABLE")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_valid_status_file() -> CheckResult:
    name = "deploy_status: valid .deploy-status.json displayed"
    import asyncio
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = asyncio.run(_run_deploy_status(SAMPLE_STATUS, Path(tmpdir)))
        checks = []
        if "2026-06-11" not in output:
            checks.append("missing deploy time")
        if "abc1234" not in output:
            checks.append("missing git sha")
        if "passed" not in output:
            checks.append("missing smoke result")
        if "active" not in output:
            checks.append("missing service status")
        if "compact" not in output:
            checks.append("missing progress mode")
        ok = len(checks) == 0
        return CheckResult(name, ok, "; ".join(checks) if ok else "; ".join(checks))
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_missing_status_file() -> CheckResult:
    name = "deploy_status: missing file returns '暂无部署状态记录'"
    import asyncio
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = asyncio.run(_run_deploy_status(None, Path(tmpdir)))
        ok = "暂无部署状态记录" in output
        return CheckResult(name, ok, f"output contains expected text: {ok}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_invalid_json_no_crash() -> CheckResult:
    name = "deploy_status: invalid JSON does not crash"
    import asyncio
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / ".deploy-status.json"
            status_file.write_text("{bad json!!", encoding="utf-8")
            output = asyncio.run(_run_deploy_status(None, Path(tmpdir)))
            # If we get here, it didn't crash. But the file won't be found
            # because _run_deploy_status(None) doesn't write it.
            # Let's test differently - write invalid json directly.
        # Direct test: write invalid JSON and call handler
        with tempfile.TemporaryDirectory() as tmpdir:
            status_file = Path(tmpdir) / ".deploy-status.json"
            status_file.write_text("{bad json!!", encoding="utf-8")

            from handlers.commands import _deploy_status
            import os
            msg = MagicMock()
            port = MagicMock()
            port.reply = AsyncMock()
            runner = MagicMock()
            settings = SimpleNamespace(conveyor_progress_mode="compact")

            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                asyncio.run(_deploy_status(msg, port, runner, settings, ""))
            finally:
                os.chdir(old_cwd)

            port.reply.assert_awaited_once()
            output = port.reply.call_args[0][1]
            # Should contain error message, not crash
            ok = "读取失败" in output or "暂无" in output or "Deploy" in output
            return CheckResult(name, ok, f"handled invalid JSON gracefully: {ok}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_env_leak() -> CheckResult:
    name = "deploy_status: no .env contents in output"
    import asyncio
    try:
        status_with_secrets = {
            **SAMPLE_STATUS,
            "secret_note": "TELEGRAM_BOT_TOKEN=123:ABC",
            "env_content": "API_KEY=sk-very-secret",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            output = asyncio.run(_run_deploy_status(status_with_secrets, Path(tmpdir)))
        # The handler reads specific fields, so secret_note/env_content
        # won't appear. But verify no bot token pattern leaked.
        has_token = "123:ABC" in output or "sk-very-secret" in output
        return CheckResult(name, not has_token, "no secrets leaked" if not has_token else "SECRETS LEAKED!")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_sh_writes_json_fields() -> CheckResult:
    name = "deploy (rsync): deploy.sh writes JSON with required fields"
    try:
        deploy_sh = REPO / "scripts" / "deploy.sh"
        content = deploy_sh.read_text(encoding="utf-8")
        required_fields = ["deployed_at", "source", "git_sha", "smoke", "services"]
        missing = [f for f in required_fields if f'"{f}"' not in content and f"'{f}'" not in content]
        ok = len(missing) == 0
        return CheckResult(name, ok, f"missing={missing}" if missing else "all fields present")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_vps_writes_json_fields() -> CheckResult:
    name = "deploy (vps): deploy_vps.sh writes JSON with required fields"
    try:
        deploy_vps = REPO / "scripts" / "deploy_vps.sh"
        content = deploy_vps.read_text(encoding="utf-8")
        required_fields = ["deployed_at", "source", "git_sha", "smoke", "services"]
        missing = [f for f in required_fields if f'"{f}"' not in content and f"'{f}'" not in content]
        ok = len(missing) == 0
        return CheckResult(name, ok, f"missing={missing}" if missing else "all fields present")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_help_mentions_deploy_status() -> CheckResult:
    name = "help: mentions /deploy_status"
    try:
        from handlers.commands import _help
        import asyncio
        msg = MagicMock()
        port = MagicMock()
        port.reply = AsyncMock()
        runner = MagicMock()
        settings = SimpleNamespace()
        asyncio.run(_help(msg, port, runner, settings, ""))
        output = port.reply.call_args[0][1]
        ok = "deploy_status" in output
        return CheckResult(name, ok, "/deploy_status in help text" if ok else "not in help text")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


CHECKS = [
    _test_handler_registered,
    _test_valid_status_file,
    _test_missing_status_file,
    _test_invalid_json_no_crash,
    _test_no_env_leak,
    _test_deploy_sh_writes_json_fields,
    _test_deploy_vps_writes_json_fields,
    _test_help_mentions_deploy_status,
]


def main() -> int:
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("deploy status smoke ok" if ok else "deploy status smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
