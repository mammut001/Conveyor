#!/usr/bin/env python3
"""tools_output_smoke.py — /tools readable output with groups and confirmation rules.

Run: .venv/bin/python scripts/tools_output_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from handlers.commands import run_command
from scripts.harness_common import CheckResult, print_results
from unittest import mock


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
        text="/tools",
    )


async def _test_tools_output() -> CheckResult:
    name = "behavior: /tools shows groups, /diagnose, /restart, strict confirm rule"
    try:
        port = FakeOutbound()
        await run_command("tools", _msg(), port, mock.Mock(), mock.Mock(), "")
        text = "\n".join(port.replies)
        checks = [
            "READ (立即执行)" in text,
            "WRITE (需确认)" in text,
            "/diagnose" in text,
            "/restart telegram|feishu|maintain" in text,
            "确认执行" in text,
            "好/ok/是/y" in text,
            "为什么服务器慢" in text or "诊断服务器" in text,
        ]
        ok = all(checks)
        return CheckResult(name, ok, f"checks={checks} len={len(text)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    results = [asyncio.run(_test_tools_output())]
    print_results(results)
    ok = all(r.ok for r in results)
    print("tools output smoke ok" if ok else "tools output smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
