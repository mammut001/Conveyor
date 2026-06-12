#!/usr/bin/env python3
"""telegram_command_fallback_smoke.py — new slash commands reach COMMAND_TABLE.

Verifies bot.py registers a generic COMMAND MessageHandler fallback and
that /load /tools /disk dispatch through handlers.commands like real
Telegram slash input would after the fallback.

Run: .venv/bin/python scripts/telegram_command_fallback_smoke.py
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "12345"
os.environ["CODEX_WORKSPACE_ROOT"] = "/tmp/tg-cmd-ws"
os.environ["CODEX_TASK_ROOT"] = "/tmp/tg-cmd-task"
os.environ["CODEX_MEMORY_ROOT"] = "/tmp/tg-cmd-mem"
os.environ["CODEX_BIN"] = "codex"
os.environ["USER_TIMEZONE"] = "UTC"

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import load_settings
from handlers.commands import COMMAND_TABLE
from scripts.harness_common import CheckResult, print_results

# Import the real dispatch module so we can patch its bound is_allowed symbol.
DISPATCH_MODULE = importlib.import_module("handlers.dispatch")
dispatch = DISPATCH_MODULE.dispatch

BOT_PY = Path(__file__).resolve().parents[1] / "bot.py"


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
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
        self.replies.append(text)
        return None


def _settings():
    return replace(load_settings(), telegram_allowed_user_id=12345)


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _test_bot_has_generic_command_handler() -> CheckResult:
    name = "AST: bot.py defines generic_command_cmd and COMMAND MessageHandler fallback"
    try:
        tree = ast.parse(BOT_PY.read_text(encoding="utf-8"))
        fn_names = {
            n.name
            for n in tree.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        if "generic_command_cmd" not in fn_names:
            return CheckResult(name, False, "generic_command_cmd missing")
        src = BOT_PY.read_text(encoding="utf-8")
        if "MessageHandler(filters.COMMAND, generic_command_cmd)" not in src:
            return CheckResult(name, False, "COMMAND fallback handler not wired")
        return CheckResult(name, True, "generic_command_cmd + COMMAND fallback present")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def _test_command_table_has_new_cmds() -> CheckResult:
    name = "registry: COMMAND_TABLE includes load/tools/disk/logs/service_status/git_status"
    needed = ("load", "tools", "disk", "logs", "service_status", "git_status")
    missing = [c for c in needed if c not in COMMAND_TABLE]
    return CheckResult(name, not missing, f"missing={missing}" if missing else "ok")


async def _test_slash_load() -> CheckResult:
    name = "behavior: /load via dispatch returns load snapshot"
    try:
        port = FakeOutbound()
        runner = mock.Mock()
        runner.start = mock.AsyncMock(side_effect=AssertionError("codex should not run"))
        with mock.patch.object(DISPATCH_MODULE, "is_allowed", return_value=True):
            await dispatch(_msg("/load"), port, settings=_settings(), runner=runner)
        runner.start.assert_not_called()
        ok = any("负载" in r or "VPS" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies[:1]!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_slash_tools() -> CheckResult:
    name = "behavior: /tools via dispatch lists agent tools"
    try:
        port = FakeOutbound()
        with mock.patch.object(DISPATCH_MODULE, "is_allowed", return_value=True):
            await dispatch(_msg("/tools"), port, settings=_settings(), runner=mock.Mock())
        ok = any("Agent 工具" in r or "load" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies[:1]!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_slash_disk() -> CheckResult:
    name = "behavior: /disk via dispatch returns disk snapshot"
    try:
        port = FakeOutbound()
        with mock.patch.object(DISPATCH_MODULE, "is_allowed", return_value=True):
            await dispatch(_msg("/disk"), port, settings=_settings(), runner=mock.Mock())
        ok = any("磁盘" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies[:1]!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    sync = [_test_bot_has_generic_command_handler, _test_command_table_has_new_cmds]
    async_fns = [_test_slash_load, _test_slash_tools, _test_slash_disk]
    results = [fn() for fn in sync]
    for fn in async_fns:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("telegram command fallback smoke ok" if ok else "telegram command fallback smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
