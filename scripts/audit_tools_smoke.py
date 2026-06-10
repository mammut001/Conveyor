#!/usr/bin/env python3
"""audit_tools_smoke.py — /audit_tools reads recent audit JSONL.

Run: .venv/bin/python scripts/audit_tools_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-audittools-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-audittools-task")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-audittools-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import load_settings
from handlers.commands import run_command
from handlers.tools.audit import audit_tool_event
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)

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


def _msg() -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m-1",
        text="/audit_tools",
    )


async def _test_empty_audit() -> CheckResult:
    name = "behavior: /audit_tools with no log returns 暂无"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = replace(load_settings(), codex_memory_root=Path(td))
            port = FakeOutbound()
            await run_command("audit_tools", _msg(), port, mock.Mock(), settings, "")
            ok = any("暂无" in r for r in port.replies)
            return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_reads_tail() -> CheckResult:
    name = "behavior: /audit_tools returns recent records"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = replace(load_settings(), codex_memory_root=Path(td))
            audit_tool_event(
                settings,
                operator_id="12345",
                chat_id="chat-1",
                channel="telegram",
                tool_name="service_restart",
                arg="conveyor-telegram-bot",
                danger="write",
                action="requested",
            )
            port = FakeOutbound()
            await run_command("audit_tools", _msg(), port, mock.Mock(), settings, "5")
            text = "\n".join(port.replies)
            ok = "service_restart" in text and "requested" in text
            ok = ok and "sk-" not in text  # redact sanity
            return CheckResult(name, ok, text[:200])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    results = [asyncio.run(fn()) for fn in [_test_empty_audit, _test_reads_tail]]
    print_results(results)
    ok = all(r.ok for r in results)
    print("audit_tools smoke ok" if ok else "audit_tools smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
