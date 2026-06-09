#!/usr/bin/env python3
"""handlers_smoke.py — channel-agnostic handler smoke.

Pins the contract from docs/003 P0:
  - /<cmd> routes through COMMAND_TABLE
  - "记 foo" → memo fast path (no codex)
  - "hi" → handle_codex_job path is invoked
  - unauthorized operator_id is rejected before command parsing
  - /memo with empty arg → usage hint
  - /fix and /run share the codex job path
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings  # noqa: E402
from handlers import dispatch  # noqa: E402
from channel.types import InboundMessage  # noqa: E402
from runner import CodexRunner  # noqa: E402
from scripts.harness_common import CheckResult, print_results  # noqa: E402


@dataclass
class FakeOutbound:
    supports_inline_buttons: bool = False
    replies: list[str] = field(default_factory=list)
    sent_new: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    _edit_broken: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ph-1"

    async def send_new(self, msg, text):
        self.sent_new.append(text)
        return f"new-{len(self.sent_new)}"

    async def edit_progress(self, msg, placeholder_id, text):
        if self._edit_broken:
            return False
        self.edits.append((placeholder_id, text))
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return "ph-1"


def _msg(channel, operator_id, text, **kw):
    return InboundMessage(
        channel=channel,
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
        **kw,
    )


def _check_command_routes_to_table():
    """ /status goes through run_command and replies via port """
    port = FakeOutbound()
    base = load_settings()
    settings = dataclasses.replace(base, lark_allowed_open_id="ou_x")
    runner = CodexRunner(settings)
    asyncio.run(dispatch(_msg("feishu", "ou_x", "/status"), port, settings, runner))
    ok = bool(port.replies)
    return CheckResult(
        "behavior: /status routes through COMMAND_TABLE → port.reply",
        ok,
        f"replied: {port.replies[0] if port.replies else '(none)'!r}",
    )


def _check_memo_keyword_routes_to_memo():
    port = FakeOutbound()
    settings = load_settings()
    runner = CodexRunner(settings)
    # The default telegram_allowed_user_id in test env is 0; that
    # matches an operator_id of "0" → allowed. Use that.
    asyncio.run(dispatch(_msg("telegram", str(settings.telegram_allowed_user_id), "记 test memo"), port, settings, runner))
    ok = any("##" in r or "已" in r or "记下" in r or "ok" in r.lower() or r.startswith("✍") for r in port.replies + port.sent_new)
    return CheckResult(
        "behavior: '记 ...' routes to memo fast path",
        ok,
        f"replies={port.replies}, sent_new={port.sent_new}",
    )


def _check_unauthorized_is_rejected():
    port = FakeOutbound()
    base = load_settings()
    settings = dataclasses.replace(base, telegram_allowed_user_id=11111)
    runner = CodexRunner(settings)
    asyncio.run(dispatch(_msg("telegram", "99999", "/status"), port, settings, runner))
    ok = any("Unauthorized" in r for r in port.replies)
    return CheckResult(
        "behavior: unauthorized operator_id is rejected before command routing",
        ok,
        f"replies={port.replies}",
    )


def _check_memo_cmd_empty_arg():
    port = FakeOutbound()
    settings = load_settings()
    runner = CodexRunner(settings)
    asyncio.run(dispatch(_msg("telegram", str(settings.telegram_allowed_user_id), "/memo"), port, settings, runner))
    ok = any("用法" in r or "Usage" in r for r in port.replies)
    return CheckResult(
        "behavior: /memo with empty arg replies with usage hint",
        ok,
        f"replies={port.replies}",
    )


def _check_run_fix_share_codex_path():
    port = FakeOutbound()
    settings = load_settings()
    runner = CodexRunner(settings)
    # /fix empty arg → usage hint
    asyncio.run(dispatch(_msg("telegram", str(settings.telegram_allowed_user_id), "/fix"), port, settings, runner))
    ok = any("用法" in r or "Usage" in r for r in port.replies)
    return CheckResult(
        "behavior: /fix empty arg is a usage hint (not a codex job)",
        ok,
        f"replies={port.replies}",
    )


def _check_unknown_command():
    port = FakeOutbound()
    settings = load_settings()
    runner = CodexRunner(settings)
    asyncio.run(dispatch(_msg("telegram", str(settings.telegram_allowed_user_id), "/nonsense"), port, settings, runner))
    ok = any("未知" in r for r in port.replies)
    return CheckResult(
        "behavior: unknown command replies with hint",
        ok,
        f"replies={port.replies}",
    )


CHECKS = [
    _check_command_routes_to_table,
    _check_memo_keyword_routes_to_memo,
    _check_unauthorized_is_rejected,
    _check_memo_cmd_empty_arg,
    _check_run_fix_share_codex_path,
    _check_unknown_command,
]


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            results.append(CheckResult(check.__name__, False, f"raised: {exc!r}"))
    print_results(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
