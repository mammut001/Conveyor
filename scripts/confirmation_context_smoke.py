#!/usr/bin/env python3
"""confirmation_context_smoke.py — pending confirmations bound to chat+channel.

Run: .venv/bin/python scripts/confirmation_context_smoke.py
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
from handlers.tools.confirm import clear_all_pending, create_pending
from handlers.tools.runner import execute_confirmed, try_resolve_confirmation
from scripts.harness_common import CheckResult, print_results


def _audit_settings() -> SimpleNamespace:
    return SimpleNamespace(codex_memory_root=Path(tempfile.mkdtemp(prefix="confirm-ctx-")))


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


def _msg(text: str, chat_id: str = "chat-1") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id=chat_id,
        message_id="m-1",
        text=text,
    )


async def _test_cross_chat_rejected() -> CheckResult:
    name = "behavior: same operator different chat cannot confirm"
    try:
        clear_all_pending()
        port = FakeOutbound()
        pending = create_pending("service_restart", "conveyor-telegram-bot", "12345", "chat-A", "telegram")
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(side_effect=AssertionError("no exec"))):
            handled = await try_resolve_confirmation(_msg("确认执行", chat_id="chat-B"), port, _audit_settings())
        from handlers.tools.confirm import get_pending
        still = get_pending(pending.token) is not None
        ok = not handled and still
        return CheckResult(name, ok, f"handled={handled} still_pending={still}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_same_chat_confirms() -> CheckResult:
    name = "behavior: same operator same chat can confirm"
    try:
        clear_all_pending()
        port = FakeOutbound()
        create_pending("service_restart", "conveyor-telegram-bot", "12345", "chat-A", "telegram")
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(return_value="ok")):
            handled = await try_resolve_confirmation(_msg("确认执行", chat_id="chat-A"), port, _audit_settings())
        ok = handled and any("ok" in r for r in port.replies)
        return CheckResult(name, ok, f"handled={handled} replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_inline_wrong_chat() -> CheckResult:
    name = "behavior: execute_confirmed rejects wrong chat (inline path)"
    try:
        clear_all_pending()
        port = FakeOutbound()
        pending = create_pending("service_restart", "conveyor-feishu-bot", "12345", "chat-A", "telegram")
        wrong = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id="chat-B",
            message_id="m-2",
            text="",
        )
        with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(side_effect=AssertionError("no exec"))):
            handled = await execute_confirmed(wrong, port, _audit_settings(), pending.token)
        ok = handled and any("Unauthorized" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    async_fns = [
        _test_cross_chat_rejected,
        _test_same_chat_confirms,
        _test_inline_wrong_chat,
    ]
    results = [asyncio.run(fn()) for fn in async_fns]
    print_results(results)
    ok = all(r.ok for r in results)
    print("confirmation context smoke ok" if ok else "confirmation context smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
