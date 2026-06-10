#!/usr/bin/env python3
"""tool_audit_smoke.py — WRITE tool audit JSONL at codex_memory_root/audit/tools.log.

Run: .venv/bin/python scripts/tool_audit_smoke.py
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
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-audit-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-audit-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

import handlers.tools.executors  # noqa: F401
from channel import InboundMessage
from config import Settings, load_settings
from handlers.tools.audit import audit_log_path, read_audit_tail
from handlers.tools.confirm import clear_all_pending
from handlers.tools.runner import _request_confirmation, cancel_pending, execute_confirmed
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = False

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


def _settings(tmp: Path) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp, telegram_allowed_user_id=12345)


async def _test_audit_request_confirm_cancel() -> CheckResult:
    name = "behavior: service_restart writes requested/confirmed/executed/cancelled"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _request_confirmation(_msg(), port, settings, "service_restart", "conveyor-telegram-bot")
            path = audit_log_path(settings)
            if not path.is_file():
                return CheckResult(name, False, "no audit file after request")
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            rec = json.loads(lines[-1])
            if rec.get("action") != "requested":
                return CheckResult(name, False, f"last action={rec.get('action')}")

            from handlers.tools.confirm import get_pending_for_context
            pending = get_pending_for_context("12345", "chat-1", "telegram")
            assert pending is not None

            with mock.patch("handlers.tools.runner.run_tool", mock.AsyncMock(return_value="restarted")):
                await execute_confirmed(_msg(), port, settings, pending.token)
            actions = [json.loads(l)["action"] for l in path.read_text().strip().splitlines()]
            if "confirmed" not in actions or "executed" not in actions:
                return CheckResult(name, False, f"actions={actions}")

            clear_all_pending()
            await _request_confirmation(_msg(), port, settings, "service_restart", "conveyor-feishu-bot")
            pending2 = get_pending_for_context("12345", "chat-1", "telegram")
            assert pending2 is not None
            await cancel_pending(_msg("取消"), port, settings, pending2.token)
            actions2 = [json.loads(l)["action"] for l in path.read_text().strip().splitlines()]
            ok = "cancelled" in actions2
            return CheckResult(name, ok, f"actions={actions2}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_audit_failure_non_fatal() -> CheckResult:
    name = "behavior: audit write failure does not break confirmation request"
    try:
        clear_all_pending()
        settings = _settings(Path("/tmp"))
        port = FakeOutbound()
        with mock.patch("handlers.tools.audit.audit_tool_event", side_effect=OSError("disk full")):
            await _request_confirmation(_msg(), port, settings, "service_restart", "conveyor-telegram-bot")
        ok = any("危险操作需确认" in r for r in port.replies)
        return CheckResult(name, ok, f"replies={port.replies}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    async_fns = [_test_audit_request_confirm_cancel, _test_audit_failure_non_fatal]
    results = [asyncio.run(fn()) for fn in async_fns]
    print_results(results)
    ok = all(r.ok for r in results)
    print("tool audit smoke ok" if ok else "tool audit smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
