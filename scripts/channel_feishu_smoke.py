#!/usr/bin/env python3
"""channel_feishu_smoke.py — env-free Feishu adapter unit tests.

Uses fake Feishu sender/client objects (no real lark_oapi network).
Covers:
  - FeishuOutbound.reply / send_new send interactive cards with
    update_multi=True and return message_id.
  - edit_progress calls update_card with the stored message_id.
  - edit_progress returns False when placeholder_id is None.
  - reply_with_buttons sends a card (safe fallback, no crash).
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


class _SendResult:
    """Minimal stand-in for lark_oapi SendResult."""

    def __init__(self, message_id: str = "", ok: bool = True, error: str = ""):
        self.message_id = message_id
        self.ok = ok
        self.error = error
        self.fail = not ok


class _FakeChannel:
    """Minimal stand-in for lark_oapi.channel.FeishuChannel."""

    def __init__(self) -> None:
        self.send_calls: list[tuple[str, dict, dict | None]] = []
        self.update_calls: list[tuple[str, dict]] = []
        self._next_msg_id = "om_new_1"

    async def send(self, chat_id, payload, opts=None):
        self.send_calls.append((chat_id, payload, opts))
        return _SendResult(message_id=self._next_msg_id, ok=True)

    async def update_card(self, message_id, card):
        self.update_calls.append((message_id, card))
        return _SendResult(ok=True)


class _FailChannel:
    """Channel where card send fails (fallback to text)."""

    def __init__(self) -> None:
        self.send_calls: list[tuple[str, dict, dict | None]] = []

    async def send(self, chat_id, payload, opts=None):
        self.send_calls.append((chat_id, payload, opts))
        # First call (card) fails; second call (text fallback) succeeds.
        if len(self.send_calls) == 1:
            return _SendResult(ok=False, error="card not supported")
        return _SendResult(message_id="om_fallback", ok=True)

    async def update_card(self, message_id, card):
        return _SendResult(ok=False, error="no card support")


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


def _test_outbound_reply_sends_card() -> CheckResult:
    name = "FeishuOutbound.reply: sends card with update_multi=True, returns message_id"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_1", message_id="om_1"))
    out = asyncio.run(port.reply(inbound, "hello"))
    ok = (
        out == "om_new_1"
        and len(channel.send_calls) == 1
        and channel.send_calls[0][0] == "oc_1"
        and channel.send_calls[0][1].get("config", {}).get("update_multi") is True
        and channel.send_calls[0][2] == {"reply_to": "om_1"}
    )
    return CheckResult(name, ok, f"msg_id={out} calls={len(channel.send_calls)}")


def _test_outbound_send_new_returns_id() -> CheckResult:
    name = "FeishuOutbound.send_new: sends card, returns message_id"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_2"))
    out = asyncio.run(port.send_new(inbound, "fresh"))
    ok = (
        out == "om_new_1"
        and len(channel.send_calls) == 1
        and channel.send_calls[0][2] is None
    )
    return CheckResult(name, ok, f"msg_id={out} calls={len(channel.send_calls)}")


def _test_outbound_no_chat_id_skips_send() -> CheckResult:
    name = "FeishuOutbound: skips channel.send when chat_id is empty"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id=""))
    asyncio.run(port.send_new(inbound, "ignored"))
    return CheckResult(name, channel.send_calls == [], f"calls={channel.send_calls}")


def _test_edit_progress_calls_update_card() -> CheckResult:
    name = "FeishuOutbound.edit_progress: calls update_card with message_id, returns True"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event())
    out = asyncio.run(port.edit_progress(inbound, "om_existing", "updated text"))
    ok = (
        out is True
        and len(channel.update_calls) == 1
        and channel.update_calls[0][0] == "om_existing"
        and channel.update_calls[0][1].get("config", {}).get("update_multi") is True
    )
    return CheckResult(name, ok, f"out={out} update_calls={len(channel.update_calls)}")


def _test_edit_progress_none_placeholder() -> CheckResult:
    name = "FeishuOutbound.edit_progress: returns False when placeholder_id is None"
    channel = _FakeChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event())
    out = asyncio.run(port.edit_progress(inbound, None, "text"))
    return CheckResult(name, out is False and channel.update_calls == [], f"out={out}")


def _test_edit_progress_failure_returns_false() -> CheckResult:
    name = "FeishuOutbound.edit_progress: returns False when update_card fails"
    channel = _FailChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event())
    out = asyncio.run(port.edit_progress(inbound, "om_bad", "text"))
    return CheckResult(name, out is False, f"out={out}")


def _test_card_fallback_to_text() -> CheckResult:
    name = "FeishuOutbound: falls back to plain text when card send fails"
    channel = _FailChannel()
    port = FeishuOutbound(channel)
    inbound = inbound_from_event(_fake_event(chat_id="oc_3", message_id="om_3"))
    out = asyncio.run(port.reply(inbound, "hello"))
    ok = (
        out is None  # fallback returns no message_id
        and len(channel.send_calls) == 2  # card attempt + text fallback
        and channel.send_calls[1][1] == {"text": "hello"}
    )
    return CheckResult(name, ok, f"out={out} calls={len(channel.send_calls)}")


def _test_reply_with_buttons_sends_card() -> CheckResult:
    name = "FeishuOutbound.reply_with_buttons: sends card (safe fallback)"
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
        out == "om_new_1"
        and len(channel.send_calls) == 1
        and channel.send_calls[0][0] == "oc_3"
        and channel.send_calls[0][1].get("config", {}).get("update_multi") is True
    )
    return CheckResult(name, ok, f"out={out} calls={len(channel.send_calls)}")


def main() -> int:
    results = [
        _test_inbound_basic(),
        _test_inbound_group_mentioned(),
        _test_inbound_unknown_chat_type(),
        _test_inbound_text_stripped(),
        _test_outbound_reply_sends_card(),
        _test_outbound_send_new_returns_id(),
        _test_outbound_no_chat_id_skips_send(),
        _test_edit_progress_calls_update_card(),
        _test_edit_progress_none_placeholder(),
        _test_edit_progress_failure_returns_false(),
        _test_card_fallback_to_text(),
        _test_reply_with_buttons_sends_card(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("channel feishu smoke ok" if ok else "channel feishu smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
