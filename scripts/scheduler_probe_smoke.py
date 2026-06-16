#!/usr/bin/env python3
"""scheduler_probe_smoke.py — P3.2.1 scheduler observability + probe smoke.

Run: .venv/bin/python scripts/scheduler_probe_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-pt-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-pt-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from config import Settings, load_settings
from handlers.commands import COMMAND_TABLE, run_command
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.registry import TOOL_REGISTRY, DangerLevel
from handlers.tools.runner import _invoke_tool, run_tool
from personal_tools.store import PersonalToolsStore
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return None

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return None


class FakeRunner:
    settings: Settings | None = None


def _msg(text: str = "", operator_id: str = "12345") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _settings(tmp: Path) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- Test 1: scheduler_status without systemctl degrades gracefully ----

async def test_scheduler_status_no_systemctl() -> CheckResult:
    name = "scheduler_status: degrades gracefully without systemctl"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            with mock.patch("scripts.scheduler_probe.shutil.which", return_value=None):
                result = await run_tool(settings, "scheduler_status")
            has_warning = "systemctl 不可用" in result
            has_counts = "pending:" in result
            has_channel = "Telegram: ✅" in result
            ok = has_warning and has_counts and has_channel
            return CheckResult(name, ok, f"warning={has_warning} counts={has_counts} channel={has_channel}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 2: scheduler_probe dry-run succeeds ----

async def test_scheduler_probe_dryrun() -> CheckResult:
    name = "scheduler_probe: dry-run succeeds without network"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            store.create_reminder("u1", "test reminder", past, channel="telegram", chat_id="999")

            with mock.patch("scripts.scheduler_probe.load_settings", return_value=settings):
                with mock.patch("scripts.scheduler_tick.load_settings", return_value=settings):
                    result = await run_tool(settings, "scheduler_probe")
            has_dryrun = "dry-run" in result
            has_pending = "pending=" in result or "待投递" in result
            ok = has_dryrun and has_pending
            return CheckResult(name, ok, f"dryrun={has_dryrun} pending={has_pending}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 3: scheduler_probe_live requires confirmation ----

async def test_scheduler_probe_live_needs_confirm() -> CheckResult:
    name = "scheduler_probe_live: requests confirmation"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "scheduler_probe_live", "")
            has_confirm_text = any("需确认" in r or "确认" in r for r in port.replies)
            pending = get_pending_for_context("12345", "chat-1", "telegram")
            ok = has_confirm_text and pending is not None
            return CheckResult(name, ok, f"confirm_text={has_confirm_text} pending={pending is not None}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 4: /tools lists new tools ----

async def test_tools_lists_new_tools() -> CheckResult:
    name = "/tools: lists scheduler tools under correct danger levels"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            await run_command("tools", _msg("/tools"), port, runner, settings, "")
            text = "\n".join(port.replies)
            has_status = "scheduler_status" in text
            has_probe = "scheduler_probe" in text
            has_probe_live = "scheduler_probe_live" in text
            ok = has_status and has_probe and has_probe_live
            return CheckResult(name, ok, f"status={has_status} probe={has_probe} live={has_probe_live}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 5: /help includes new commands ----

async def test_help_includes_new_commands() -> CheckResult:
    name = "/help: includes scheduler commands"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            await run_command("help", _msg("/help"), port, runner, settings, "")
            text = "\n".join(port.replies)
            has_status = "/scheduler_status" in text
            has_probe = "/scheduler_probe" in text
            has_probe_live = "/scheduler_probe_live" in text
            ok = has_status and has_probe and has_probe_live
            return CheckResult(name, ok, f"status={has_status} probe={has_probe} live={has_probe_live}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 6: tool registry has correct danger levels ----

def test_registry_danger_levels() -> CheckResult:
    name = "registry: scheduler tools have correct danger levels"
    try:
        status_spec = TOOL_REGISTRY.get("scheduler_status")
        probe_spec = TOOL_REGISTRY.get("scheduler_probe")
        live_spec = TOOL_REGISTRY.get("scheduler_probe_live")
        ok_status = status_spec is not None and status_spec.danger == DangerLevel.READ
        ok_probe = probe_spec is not None and probe_spec.danger == DangerLevel.READ
        ok_live = live_spec is not None and live_spec.danger == DangerLevel.WRITE
        ok = ok_status and ok_probe and ok_live
        return CheckResult(name, ok, f"status={ok_status} probe={ok_probe} live={ok_live}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 7: commands are registered ----

def test_commands_registered() -> CheckResult:
    name = "COMMAND_TABLE: scheduler commands registered"
    try:
        has_status = "scheduler_status" in COMMAND_TABLE
        has_probe = "scheduler_probe" in COMMAND_TABLE
        has_probe_live = "scheduler_probe_live" in COMMAND_TABLE
        ok = has_status and has_probe and has_probe_live
        return CheckResult(name, ok, f"status={has_status} probe={has_probe} live={has_probe_live}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- main ----

async def main() -> int:
    results = [
        test_registry_danger_levels(),
        test_commands_registered(),
        await test_scheduler_status_no_systemctl(),
        await test_scheduler_probe_dryrun(),
        await test_scheduler_probe_live_needs_confirm(),
        await test_tools_lists_new_tools(),
        await test_help_includes_new_commands(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("scheduler probe smoke ok" if ok else "scheduler probe smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
