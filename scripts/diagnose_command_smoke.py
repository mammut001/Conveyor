#!/usr/bin/env python3
"""diagnose_command_smoke.py — /diagnose hybrid path collects facts then Codex.

Run: .venv/bin/python scripts/diagnose_command_smoke.py
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
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-diag-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-diag-task")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-diag-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import load_settings
from handlers.commands import run_command
from handlers.tools.diagnose import DIAGNOSE_MODES, normalize_diagnose_mode
from handlers.tools.runner import handle_diagnose_command
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


def _test_normalize_modes() -> CheckResult:
    name = "diagnose: normalize_diagnose_mode defaults and validates"
    ok = (
        normalize_diagnose_mode("") == "server"
        and normalize_diagnose_mode("quick") == "quick"
        and normalize_diagnose_mode("unknown") == ""
    )
    return CheckResult(name, ok, f"server={normalize_diagnose_mode('')!r}")


async def _test_diagnose_server_calls_codex_with_facts() -> CheckResult:
    name = "behavior: /diagnose server collects tools then Codex prompt has facts"
    try:
        port = FakeOutbound()
        captured: dict[str, str] = {}

        async def fake_collected(_settings, items):
            names = [n for n, _ in items]
            return f"FAKE_FACTS tools={','.join(names)}"

        async def fake_codex(msg, port, runner, *, mode, prompt):
            captured["prompt"] = prompt

        with mock.patch("handlers.tools.runner.run_tools_collected", fake_collected):
            with mock.patch("handlers.tools.runner.handle_codex_job", fake_codex):
                await handle_diagnose_command(_msg(), port, mock.Mock(), _settings(), "")
        prompt = captured.get("prompt", "")
        expected = {n for n, _ in DIAGNOSE_MODES["server"]}
        got = set(prompt.split("tools=")[-1].split()[0].split(",")) if "FAKE_FACTS" in prompt else set()
        ok = "FAKE_FACTS" in prompt and expected <= got and "不要编造" in prompt
        return CheckResult(name, ok, f"prompt_len={len(prompt)} tools={got}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_diagnose_quick_fewer_tools() -> CheckResult:
    name = "behavior: /diagnose quick uses fewer tools than server"
    try:
        collected: list[tuple[tuple[str, str], ...]] = []

        async def fake_collected(_settings, items):
            collected.append(items)
            return "FACTS"

        async def fake_codex(*_a, **_k):
            return None

        with mock.patch("handlers.tools.runner.run_tools_collected", fake_collected):
            with mock.patch("handlers.tools.runner.handle_codex_job", fake_codex):
                await handle_diagnose_command(_msg(), FakeOutbound(), mock.Mock(), _settings(), "quick")
        server_n = len(DIAGNOSE_MODES["server"])
        quick_n = len(collected[0]) if collected else 0
        ok = quick_n > 0 and quick_n < server_n
        return CheckResult(name, ok, f"quick={quick_n} server={server_n}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_diag_command_unchanged() -> CheckResult:
    name = "behavior: /diag still calls diagnostics_report (not hybrid diagnose)"
    try:
        port = FakeOutbound()
        with mock.patch(
            "handlers.commands.diagnostics_report",
            return_value="diag report ok",
        ) as diag:
            handled = await run_command("diag", _msg("/diag"), port, mock.Mock(), _settings(), "")
            diag.assert_called_once()
        ok = handled and any("diag report" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    sync = [_test_normalize_modes]
    async_fns = [
        _test_diagnose_server_calls_codex_with_facts,
        _test_diagnose_quick_fewer_tools,
        _test_diag_command_unchanged,
    ]
    results = [fn() for fn in sync]
    for fn in async_fns:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("diagnose command smoke ok" if ok else "diagnose command smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
