#!/usr/bin/env python3
"""restart_alias_smoke.py — /restart aliases require confirmation.

Run: .venv/bin/python scripts/restart_alias_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-restart-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-restart-task")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-restart-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import load_settings
from handlers.commands import run_command
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.restart_aliases import resolve_restart_alias
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    button_replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = True

    async def reply(self, msg, text):
        self.replies.append(text)
        return None

    async def send_new(self, msg, text):
        self.replies.append(text)
        return None

    async def edit_progress(self, msg, placeholder_id, text):
        return False

    async def reply_with_buttons(self, msg, text, buttons):
        self.button_replies.append(text)
        return None


def _msg(text: str = "") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _settings():
    return replace(load_settings(), telegram_allowed_user_id=12345)


def _test_alias_map() -> CheckResult:
    name = "restart: telegram -> conveyor-telegram-bot"
    unit = resolve_restart_alias("telegram")
    return CheckResult(name, unit == "conveyor-telegram-bot", f"got {unit!r}")


async def _test_restart_telegram_pending() -> CheckResult:
    name = "behavior: /restart telegram creates pending, does not execute"
    try:
        clear_all_pending()
        port = FakeOutbound()
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(side_effect=AssertionError("no exec"))):
            await run_command("restart", _msg("/restart telegram"), port, mock.Mock(), _settings(), "telegram")
        pending = get_pending_for_context("12345", "chat-1", "telegram")
        ok = pending is not None and pending.tool_name == "service_restart"
        ok = ok and pending.arg == "conveyor-telegram-bot"
        ok = ok and any("conveyor-telegram-bot" in t for t in port.button_replies)
        return CheckResult(name, ok, f"pending={pending!r} buttons={port.button_replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_restart_unknown_usage() -> CheckResult:
    name = "behavior: /restart unknown returns usage"
    try:
        clear_all_pending()
        port = FakeOutbound()
        await run_command("restart", _msg("/restart foo"), port, mock.Mock(), _settings(), "foo")
        ok = any("telegram|feishu|maintain" in r for r in port.replies)
        ok = ok and get_pending_for_context("12345", "chat-1", "telegram") is None
        return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    sync = [_test_alias_map]
    async_fns = [_test_restart_telegram_pending, _test_restart_unknown_usage]
    results = [fn() for fn in sync]
    for fn in async_fns:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("restart alias smoke ok" if ok else "restart alias smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
