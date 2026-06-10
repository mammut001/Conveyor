#!/usr/bin/env python3
"""confirm_strict_smoke.py — dangerous tool text confirmation is strict.

Run: .venv/bin/python scripts/confirm_strict_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel import InboundMessage
from handlers.tools.confirm import (
    clear_all_pending,
    create_pending,
    is_confirmation_text,
    is_cancellation_text,
)
from handlers.tools.runner import cancel_pending, execute_confirmed, try_resolve_confirmation
from scripts.harness_common import CheckResult, print_results


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


def _msg(text: str, operator_id: str = "12345") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _audit_settings() -> SimpleNamespace:
    """Realistic settings with codex_memory_root pointing at a temp dir,
    so audit JSONL writes succeed without TypeError noise."""
    return SimpleNamespace(codex_memory_root=Path(tempfile.mkdtemp(prefix="confirm-strict-")))


def _test_ambiguous_not_confirm() -> CheckResult:
    name = "strict: 好/ok/是/y are NOT confirmation"
    bad = ("好", "ok", "是", "y", "yes", "确定")
    fails = [w for w in bad if is_confirmation_text(w)]
    return CheckResult(name, not fails, f"incorrectly accepted: {fails}")


def _test_explicit_confirm() -> CheckResult:
    name = "strict: 确认执行/confirm/execute ARE confirmation"
    good = ("确认", "确认执行", "确认重启", "confirm", "yes confirm", "execute")
    missing = [w for w in good if not is_confirmation_text(w)]
    return CheckResult(name, not missing, f"rejected: {missing}" if missing else "ok")


async def _test_hao_does_not_execute() -> CheckResult:
    name = "behavior: pending service_restart + 好 does NOT execute tool"
    try:
        clear_all_pending()
        port = FakeOutbound()
        create_pending("service_restart", "conveyor-telegram-bot", "12345", "chat-1", "telegram")
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(side_effect=AssertionError("no exec"))):
            handled = await try_resolve_confirmation(_msg("好"), port, _audit_settings())
        return CheckResult(name, not handled and not port.replies, f"handled={handled} replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_confirm_execute_runs() -> CheckResult:
    name = "behavior: pending service_restart + 确认执行 executes tool"
    try:
        clear_all_pending()
        port = FakeOutbound()
        pending = create_pending("service_restart", "conveyor-telegram-bot", "12345", "chat-1", "telegram")
        with mock.patch(
            "handlers.tools.runner.run_tool",
            mock.AsyncMock(return_value="restarted ok"),
        ) as run_tool:
            handled = await try_resolve_confirmation(_msg("确认执行"), port, _audit_settings())
            run_tool.assert_called_once()
        ok = handled and any("restarted" in r for r in port.replies)
        return CheckResult(name, ok, f"handled={handled} replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_cancel_clears() -> CheckResult:
    name = "behavior: 取消 cancels pending action"
    try:
        clear_all_pending()
        port = FakeOutbound()
        pending = create_pending("service_restart", "", "12345", "chat-1", "telegram")
        handled = await cancel_pending(_msg("取消"), port, _audit_settings(), pending.token)
        ok = handled and any("取消" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_inline_confirm_still_works() -> CheckResult:
    name = "behavior: execute_confirmed(token) runs tool (inline button path)"
    try:
        clear_all_pending()
        port = FakeOutbound()
        pending = create_pending("service_restart", "conveyor-feishu-bot", "12345", "chat-1", "telegram")
        with mock.patch(
            "handlers.tools.runner.run_tool",
            mock.AsyncMock(return_value="done"),
        ) as run_tool:
            ok_flag = await execute_confirmed(_msg(""), port, _audit_settings(), pending.token)
            run_tool.assert_called_once()
        return CheckResult(name, ok_flag and port.replies, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    sync = [_test_ambiguous_not_confirm, _test_explicit_confirm]
    async_fns = [
        _test_hao_does_not_execute,
        _test_confirm_execute_runs,
        _test_cancel_clears,
        _test_inline_confirm_still_works,
    ]
    results = [fn() for fn in sync]
    for fn in async_fns:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("confirm strict smoke ok" if ok else "confirm strict smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())