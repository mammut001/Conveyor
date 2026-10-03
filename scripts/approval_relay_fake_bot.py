#!/usr/bin/env python3
"""scripts/approval_relay_fake_bot.py — Simulates a Telegram bot process against a relay DB without network.

This script operates in 100% offline mode and never transmits network traffic:
relay notifications go to an in-memory FakeNotifier (optionally logged with
--notify-log) and approved tools are NOT really executed — a stub tool runner
returns "[fake bot] executed <tool>" so click-tests have no side effects.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import approval_relay
from channel.types import InboundMessage, OutboundPort
from handlers.tools.confirm import PendingToolAction, create_pending, get_pending
from redaction import redact_text


class FakeOutboundPort(OutboundPort):
    supports_inline_buttons: bool = True
    supports_attachments: bool = False

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        self.messages.append(text)
        return "fake_reply_id"

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        self.messages.append(text)
        return "fake_send_id"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.messages.append(text)
        return True

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list[list[dict]]) -> str | None:
        self.messages.append(text)
        return "fake_btn_id"


@dataclass
class FakeSettings:
    approval_relay_enabled: bool = True
    approval_inbox_enabled: bool = True
    approval_relay_db: Path | None = None
    approval_relay_channels: tuple[str, ...] = ()
    codex_workspace_root: str = "/tmp"
    codex_task_root: str = "/tmp"
    codex_memory_root: str = "/tmp"
    telegram_bot_token: str = "fake_test_token"
    telegram_allowed_user_id: int = 12345


def make_settings(db_path: str) -> FakeSettings:
    p = Path(db_path).resolve()
    return FakeSettings(approval_relay_db=p)


def check_network_safety(allow_network: bool = False) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    is_real = bool(token and not token.startswith("test") and not token.startswith("fake"))
    if allow_network and is_real:
        sys.stderr.write("Refusing to start: real-looking TELEGRAM_BOT_TOKEN detected with --allow-network.\n")
        sys.exit(1)


async def create_and_consume(
    settings: FakeSettings,
    tool_name: str,
    arg: str,
    summary: str = "",
    timeout: float = 10.0,
    poll_interval: float = 0.05,
) -> dict[str, Any]:
    port = FakeOutboundPort()
    action = create_pending(
        tool_name=tool_name,
        arg=arg,
        operator_id="fake_operator_123",
        chat_id="fake_chat_123",
        channel="telegram",
    )
    approval_relay.publish(
        settings,
        action,
        summary=summary or f"Run {tool_name}",
        danger="write",
        source="chat",
    )
    created_event = {
        "event": "created",
        "token": action.token,
        "tool_name": action.tool_name,
        "arg_preview": redact_text(action.arg)[:500],
    }
    print(json.dumps(created_event), flush=True)

    import handlers.tools.runner as tool_runner

    async def _stub_run_tool(_settings: Any, name: str, tool_arg: str, **_kw: Any) -> str:
        return f"[fake bot] executed {name} ({redact_text(tool_arg)[:200]})"

    tool_runner.run_tool = _stub_run_tool  # type: ignore[assignment]

    consumer = approval_relay.RelayConsumer(
        settings,
        channel="telegram",
        poll_interval=poll_interval,
        port_factory=lambda _chat_id: port,
    )

    deadline = time.time() + timeout
    result_event: dict[str, Any] = {"token": action.token, "status": "timeout"}

    while time.time() < deadline:
        await consumer.poll_once()
        row = approval_relay.get_relay_row(settings, action.token)
        if row and row["status"] in ("done", "failed", "cancelled", "expired"):
            result_event = {
                "event": "resolved",
                "token": action.token,
                "status": row["status"],
                "decided_via": row.get("decided_via"),
                "decided_by": row.get("decided_by"),
                "messages": list(port.messages),
            }
            print(json.dumps(result_event), flush=True)
            break
        await asyncio.sleep(poll_interval)

    consumer.stop()
    return result_event


def press(
    settings: FakeSettings,
    token: str,
    decision: str,
    decided_by: str = "fake_operator",
) -> str:
    approve = decision.lower() in ("approve", "yes", "confirm", "1", "true")
    outcome = approval_relay.decide(
        settings,
        token,
        approve=approve,
        via="telegram",
        decided_by=decided_by,
    )
    res = {
        "event": "pressed",
        "token": token,
        "decision": "approved" if approve else "rejected",
        "outcome": outcome,
    }
    print(json.dumps(res), flush=True)
    return outcome


def list_pending_rows(settings: FakeSettings) -> list[dict[str, Any]]:
    rows = approval_relay.list_pending(settings)
    out = []
    for r in rows:
        clean = {
            "token": r["token"],
            "tool_name": r["tool_name"],
            "summary": r["summary"],
            "arg_preview": r["arg_preview"],
            "danger": r["danger"],
            "origin_channel": r["origin_channel"],
            "status": r["status"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        out.append(clean)
        print(json.dumps(clean), flush=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Approval relay fake bot runner — simulates Telegram bot without network traffic."
    )
    parser.add_argument("--allow-network", action="store_true", help="Ignored (the script never uses the network); refused when a real-looking TELEGRAM_BOT_TOKEN is set.")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    create_p = subparsers.add_parser("create", help="Create a pending action and wait for decision.")
    create_p.add_argument("--db", required=True, help="Path to relay SQLite database.")
    create_p.add_argument("--tool", default="notes.add", help="Tool name to create (default notes.add).")
    create_p.add_argument("--arg", default="test note", help="Tool argument.")
    create_p.add_argument("--summary", default="", help="Summary text.")
    create_p.add_argument("--timeout", type=float, default=10.0, help="Wait timeout in seconds.")

    press_p = subparsers.add_parser("press", help="Simulate button press (approve/reject).")
    press_p.add_argument("--db", required=True, help="Path to relay SQLite database.")
    press_p.add_argument("--token", required=True, help="Approval token.")
    press_p.add_argument("decision", choices=["approve", "reject"], help="Decision to submit.")

    list_p = subparsers.add_parser("list", help="List pending relay approvals.")
    list_p.add_argument("--db", required=True, help="Path to relay SQLite database.")

    parser.add_argument("--notify-log", default="", help="Append fake notification sends/updates as JSON lines here.")
    args = parser.parse_args()
    check_network_safety(args.allow_network)
    fake_notifiers = {
        ch: approval_relay.FakeNotifier(ch, args.notify_log or None) for ch in ("telegram", "feishu")
    }
    approval_relay.set_notifier_factory(lambda ch, _s: fake_notifiers.get(ch))

    settings = make_settings(args.db)

    if args.subcommand == "create":
        asyncio.run(
            create_and_consume(
                settings,
                tool_name=args.tool,
                arg=args.arg,
                summary=args.summary,
                timeout=args.timeout,
            )
        )
    elif args.subcommand == "press":
        press(settings, token=args.token, decision=args.decision)
    elif args.subcommand == "list":
        list_pending_rows(settings)


if __name__ == "__main__":
    main()
