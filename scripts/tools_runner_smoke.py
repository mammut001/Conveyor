#!/usr/bin/env python3
"""tools_runner_smoke.py — tool execution, hybrid path, confirmation policy.

Run: .venv/bin/python scripts/tools_runner_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["TELEGRAM_BOT_TOKEN"] = "fake-token"
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "12345"
os.environ["LARK_APP_ID"] = "cli_fake"
os.environ["LARK_APP_SECRET"] = "fake"
os.environ["CODEX_WORKSPACE_ROOT"] = "/tmp/codex-tools-ws"
os.environ["CODEX_TASK_ROOT"] = "/tmp/codex-tools-task"
os.environ["CODEX_MEMORY_ROOT"] = "/tmp/codex-tools-mem"
os.environ["CODEX_BIN"] = "codex"
os.environ["USER_TIMEZONE"] = "UTC"

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import Settings, load_settings
from handlers.dispatch import dispatch
from handlers.intent import RouteResult
from handlers.tools.registry import get_tool, requires_confirmation
from handlers.tools.runner import handle_hybrid, handle_route, run_tool
from runner import JobMode
from scripts.harness_common import CheckResult, print_results


def _settings() -> Settings:
    """Hermetic settings for dispatch tests (avoid .env overriding allowlist)."""
    base = load_settings()
    return replace(base, telegram_allowed_user_id=12345)


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


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _test_registry_has_core_tools() -> CheckResult:
    name = "registry: load/ps/disk/logs/service_status/git_status registered"
    needed = ("load", "ps", "disk", "logs", "service_status", "git_status")
    missing = [n for n in needed if get_tool(n) is None]
    return CheckResult(name, not missing, f"missing={missing}" if missing else f"{len(needed)} tools ok")


def _test_service_restart_requires_confirmation() -> CheckResult:
    name = "safety: service_restart requires confirmation"
    spec = get_tool("service_restart")
    if spec is None:
        return CheckResult(name, False, "tool missing")
    return CheckResult(name, requires_confirmation(spec), f"danger={spec.danger}")


async def _test_run_load_tool() -> CheckResult:
    name = "behavior: run_tool(load) returns snapshot sections"
    try:
        text = await run_tool(_settings(), "load")
        for needle in ("负载", "CPU", "内存", "磁盘"):
            if needle not in text:
                return CheckResult(name, False, f"missing {needle}")
        return CheckResult(name, True, f"len={len(text)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_dangerous_tool_shows_confirmation() -> CheckResult:
    name = "behavior: service_restart routes to confirmation buttons, not immediate exec"
    try:
        port = FakeOutbound()
        route = RouteResult(kind="deterministic", tools=("service_restart",))
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(side_effect=AssertionError("should not run"))):
            await handle_route(_msg("重启 bot"), port, mock.Mock(), _settings(), route)
        if not port.button_replies:
            return CheckResult(name, False, f"no buttons; replies={port.replies}")
        if "确认" not in port.button_replies[0]:
            return CheckResult(name, False, port.button_replies[0][:200])
        from handlers.tools.confirm import clear_all_pending
        clear_all_pending()
        return CheckResult(name, True, "confirmation UI shown")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_hybrid_calls_codex_with_facts() -> CheckResult:
    name = "behavior: hybrid collects facts then calls CodexRunner.start with facts in prompt"
    try:
        port = FakeOutbound()
        runner = mock.Mock()
        runner.settings = _settings()
        captured: dict = {}

        async def fake_start(mode, prompt, progress):
            captured["prompt"] = prompt
            from types import SimpleNamespace
            return SimpleNamespace(state="completed", summary="analysis", error=None)

        runner.start = fake_start
        route = RouteResult(
            kind="hybrid",
            tools=("load", "disk"),
            question="为什么服务器慢",
        )
        await handle_hybrid(_msg("为什么服务器慢"), port, runner, _settings(), route)
        prompt = captured.get("prompt", "")
        ok = "tool:load" in prompt and "tool:disk" in prompt and "为什么服务器慢" in prompt
        return CheckResult(name, ok, f"prompt_len={len(prompt)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_dispatch_hybrid_skips_direct_codex_for_facts_only() -> CheckResult:
    name = "behavior: dispatch hybrid invokes Codex with embedded tool facts"
    try:
        from handlers.tools.confirm import clear_all_pending
        clear_all_pending()
        port = FakeOutbound()
        captured: dict = {}

        async def fake_codex_job(msg, port, runner, mode=JobMode.RUN, prompt=None):
            captured["prompt"] = prompt

        with mock.patch(
            "handlers.tools.runner.run_tools",
            mock.AsyncMock(return_value="## tool:load\nsnapshot facts"),
        ), mock.patch("handlers.tools.runner.handle_codex_job", fake_codex_job):
            await dispatch(_msg("为什么服务器这么慢"), port, runner=mock.Mock(), settings=_settings())
        prompt = captured.get("prompt", "")
        ok = "tool:load" in prompt and "为什么服务器这么慢" in prompt
        return CheckResult(name, ok, f"prompt_len={len(prompt)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_service_restart_whitelist() -> CheckResult:
    name = "behavior: service_restart rejects unknown unit conveyor-foo"
    try:
        text = await run_tool(_settings(), "service_restart", "conveyor-foo")
        ok = ("conveyor-foo" in text and ("拒绝" in text or "只允许" in text))
        ok = ok and "已请求重启" not in text
        return CheckResult(name, ok, text[:120])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_service_restart_empty_arg_refused() -> CheckResult:
    name = "safety: service_restart(\"\") refuses without touching systemctl"
    try:
        text = await run_tool(_settings(), "service_restart", "")
        ok = ("未指定" in text or "/restart" in text) and "已请求重启" not in text
        return CheckResult(name, ok, text[:160])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_natural_lang_feishu_pending() -> CheckResult:
    name = "behavior: '重启 feishu bot' pending targets conveyor-feishu-bot"
    try:
        from handlers.tools.confirm import clear_all_pending, get_pending_for_context
        clear_all_pending()
        port = FakeOutbound()
        runner = mock.Mock()
        runner.start = mock.AsyncMock(side_effect=AssertionError("codex should not run"))
        await dispatch(_msg("重启 feishu bot"), port, settings=_settings(), runner=runner)
        pending = get_pending_for_context("12345", "chat-1", "telegram")
        ok = pending is not None and pending.arg == "conveyor-feishu-bot"
        return CheckResult(name, ok, f"pending={pending!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_natural_lang_ambiguous_no_telegram_default() -> CheckResult:
    name = "safety: '重启 bot' must NOT default to telegram"
    try:
        from handlers.tools.confirm import clear_all_pending, get_pending_for_context
        clear_all_pending()
        port = FakeOutbound()
        runner = mock.Mock()
        runner.settings = _settings()
        # If this test ever creates a pending service_restart with
        # empty arg, run_tool would refuse — but we want to fail loud
        # BEFORE that, by asserting no pending was created at all.
        async def fail(*_a, **_k):
            raise AssertionError("service_restart should not run for ambiguous restart")
        with mock.patch("handlers.tools.runner.run_tool", side_effect=fail):
            await dispatch(_msg("重启 bot"), port, settings=_settings(), runner=runner)
        pending = get_pending_for_context("12345", "chat-1", "telegram")
        ok = pending is None
        return CheckResult(name, ok, f"pending={pending!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_dispatch_disk_deterministic_no_codex() -> CheckResult:
    name = "behavior: '看看磁盘' does not call CodexRunner.start"
    try:
        from handlers.tools.confirm import clear_all_pending
        clear_all_pending()
        port = FakeOutbound()
        runner = mock.Mock()
        runner.settings = _settings()
        runner.start = mock.AsyncMock(side_effect=AssertionError("codex should not run"))
        await dispatch(_msg("看看磁盘空间"), port, settings=_settings(), runner=runner)
        runner.start.assert_not_called()
        if not any("磁盘" in r for r in port.replies):
            return CheckResult(name, False, f"replies={port.replies!r}")
        return CheckResult(name, True, "disk tool reply sent")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    sync_checks = [
        _test_registry_has_core_tools,
        _test_service_restart_requires_confirmation,
    ]
    async_checks = [
        _test_run_load_tool,
        _test_dangerous_tool_shows_confirmation,
        _test_hybrid_calls_codex_with_facts,
        _test_dispatch_hybrid_skips_direct_codex_for_facts_only,
        _test_service_restart_whitelist,
        _test_service_restart_empty_arg_refused,
        _test_natural_lang_feishu_pending,
        _test_natural_lang_ambiguous_no_telegram_default,
        _test_dispatch_disk_deterministic_no_codex,
    ]
    results = [fn() for fn in sync_checks]
    for fn in async_checks:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("tools runner smoke ok" if ok else "tools runner smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
