#!/usr/bin/env python3
"""channel_feishu_smoke.py — env-free Feishu adapter unit tests.

Uses fake Feishu sender/client objects (no real lark_oapi network).
Covers:
  - FeishuOutbound.reply / send_new call channel.send with the right
    chat_id and a text payload, replying to the original message_id
    when applicable.
  - edit_progress returns False (no streaming edit yet).
  - reply_with_buttons safely falls back to plain text (no crash on
    empty buttons, returns None).
  - inbound_from_event converts a Feishu-shaped event to InboundMessage
    with channel=feishu and the right chat_type / mentioned_bot.

Run: .venv/bin/python scripts/channel_feishu_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

from channel.feishu import (  # noqa: E402
    FeishuOutbound,
    inbound_from_event,
)


class _FakeChannel:
    """Minimal stand-in for lark_oapi.channel.FeishuChannel."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []

    async def send(self, chat_id, payload, opts=None):
        self.calls.append((chat_id, payload, opts))


def _fake_event(
    *,
    sender_id: str = "ou_user",
    chat_id: str = "oc_chat",
    message_id: str = "om_msg",
    chat_type: str = "p2p",
    text: str = "hi",
    mentioned_bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        sender_id=sender_id,
        chat_id=chat_id,
        message_id=message_id,
        chat_type=chat_type,
        content_text=text,
        mentioned_bot=mentioned_bot,
    )


def _test_inbound_basic() -> CheckResult:
    name = "inbound_from_event: maps Feishu attrs to InboundMessage(channel=feishu, p2p)"
    event = _fake_event()
    inbound = inbound_from_event(event)
    return CheckResult(
        name,
        inbound.channel == "feishu"
        and inbound.operator_id == "ou_user"
        and inbound.chat_id == "oc_chat"
        and inbound.message_id == "om_msg"
        and inbound.chat_type == "p2p"
        and inbound.text == "hi"
        and inbound.mentioned_bot is False,
        f"inbound={inbound!r}",
    )


def _test_inbound_group_mentioned() -> CheckResult:
    name = "inbound_from_event: group + mentioned_bot"
    event = _fake_event(chat_type="group", mentioned_bot=True)
    inbound = inbound_from_event(event)
    return CheckResult(
        name,
        inbound.chat_type == "group" and inbound.mentioned_bot is True,
        f"chat_type={inbound.chat_type!r} mentioned={inbound.mentioned_bot}",
    )


def _test_inbound_unknown_chat_type() -> CheckResult:
    name = "inbound_from_event: unknown chat_type → falls back to 'unknown'"
    event = _fake_event(chat_type="channel")
    inbound = inbound_from_event(event)
    return CheckResult(name, inbound.chat_type == "unknown", f"chat_type={inbound.chat_type!r}")


def _test_inbound_text_stripped() -> CheckResult:
    name = "inbound_from_event: text is stripped"
    event = _fake_event(text="   hello   ")
    return CheckResult(name, inbound_from_event(event).text == "hello", f"text={inbound_from_event(event).text!r}")


def _test_outbound_reply_threads() -> CheckResult:
    name = "FeishuOutbound.reply: sends with reply_to=original message_id"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_1", message_id="om_1"))
    out = asyncio.run(port.reply(inbound, "hello"))
    ok = (
        out is None
        and len(channel.calls) == 1
        and channel.calls[0][0] == "oc_1"
        and channel.calls[0][1] == {"text": "hello"}
        and channel.calls[0][2] == {"reply_to": "om_1"}
    )
    return CheckResult(name, ok, f"calls={channel.calls}")


def _test_outbound_send_new_no_reply() -> CheckResult:
    name = "FeishuOutbound.send_new: sends with reply_to=None (fresh message)"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_2"))
    out = asyncio.run(port.send_new(inbound, "fresh"))
    ok = (
        out is None
        and len(channel.calls) == 1
        and channel.calls[0][0] == "oc_2"
        and channel.calls[0][2] is None
    )
    return CheckResult(name, ok, f"calls={channel.calls}")


def _test_outbound_no_chat_id_skips_send() -> CheckResult:
    name = "FeishuOutbound: skips channel.send when chat_id is empty"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id=""))
    asyncio.run(port.send_new(inbound, "ignored"))
    return CheckResult(name, channel.calls == [], f"calls={channel.calls}")


def _test_outbound_edit_progress_false() -> CheckResult:
    name = "FeishuOutbound.edit_progress: always returns False (no streaming edit yet)"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event())
    out = asyncio.run(port.edit_progress(inbound, "placeholder-1", "new"))
    return CheckResult(name, out is False and channel.calls == [], f"out={out}")


def _test_outbound_reply_with_buttons_fallback() -> CheckResult:
    name = "FeishuOutbound.reply_with_buttons: safe text fallback, no exception"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_3", message_id="om_3"))
    out = asyncio.run(
        port.reply_with_buttons(
            inbound,
            "Are you sure?",
            [[
                {"text": "Confirm", "callback_data": "tool:confirm:abc"},
            ]],
        )
    )
    ok = (
        out is None
        and len(channel.calls) == 1
        and channel.calls[0][0] == "oc_3"
        and channel.calls[0][1] == {"text": "Are you sure?"}
        and channel.calls[0][2] == {"reply_to": "om_3"}
    )
    return CheckResult(name, ok, f"calls={channel.calls}")


def _test_outbound_reply_with_buttons_empty() -> CheckResult:
    name = "FeishuOutbound.reply_with_buttons: empty buttons do not crash"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event())
    try:
        out = asyncio.run(port.reply_with_buttons(inbound, "text", []))
        ok = out is None and len(channel.calls) == 1
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    return CheckResult(name, ok, f"calls={channel.calls}")


def main() -> int:
    results = [
        _test_inbound_basic(),
        _test_inbound_group_mentioned(),
        _test_inbound_unknown_chat_type(),
        _test_inbound_text_stripped(),
        _test_outbound_reply_threads(),
        _test_outbound_send_new_no_reply(),
        _test_outbound_no_chat_id_skips_send(),
        _test_outbound_edit_progress_false(),
        _test_outbound_reply_with_buttons_fallback(),
        _test_outbound_reply_with_buttons_empty(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("channel feishu smoke ok" if ok else "channel feishu smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
