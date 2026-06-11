#!/usr/bin/env python3
"""channel_telegram_smoke.py — env-free Telegram adapter unit tests.

Uses fake Update / Message / Bot objects (no real Telegram SDK
network). Covers:
  - TelegramOutbound.reply / send_new return message_id
  - edit_progress True on success, True on "not modified", False on
    bad id / generic failure
  - reply_with_buttons builds a real InlineKeyboardMarkup and returns
    a message_id
  - inbound_from_update converts user/chat/message/text to the
    correct InboundMessage (channel=telegram, chat_type=p2p|group)

Run: .venv/bin/python scripts/channel_telegram_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

from channel.telegram import (  # noqa: E402
    TelegramOutbound,
    edit_text,
    inbound_from_update,
    make_outbound,
    send_text,
)
from telegram import InlineKeyboardMarkup  # noqa: E402


def _fake_update(
    *,
    user_id: int = 1001,
    chat_id: int = 2002,
    chat_type: str = "private",
    message_text: str = "hello",
    message_id: int = 42,
    send_returns: object = 99,
    edit_raises: BaseException | None = None,
    edit_message_id_seen: list[int] | None = None,
):
    """Build a minimal Update-shaped object.

    We deliberately use SimpleNamespace / tiny classes instead of mock
    so the test fails loudly if the adapter assumes a real method
    that our fake does not provide.
    """

    class _Bot:
        def __init__(self) -> None:
            self.edits: list[tuple[int, int, str]] = []

        async def edit_message_text(
            self, *, chat_id: int, message_id: int, text: str, **_
        ):
            if edit_message_id_seen is not None:
                edit_message_id_seen.append(message_id)
            if edit_raises is not None:
                raise edit_raises
            self.edits.append((chat_id, message_id, text))
            return SimpleNamespace(message_id=message_id)

    class _Message:
        def __init__(self) -> None:
            self.text = message_text
            self.message_id = message_id
            self.sent: list[tuple[str, object, dict]] = []

        async def reply_text(self, text, **kwargs):
            self.sent.append((text, kwargs.get("reply_markup"), kwargs))
            return SimpleNamespace(
                message_id=send_returns,
            )

    bot = _Bot()
    msg = _Message()
    user = SimpleNamespace(id=user_id, username="alice")
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        effective_message=msg,
        get_bot=lambda: bot,
    )
    return update, bot, msg


def _test_inbound_p2p() -> CheckResult:
    name = "inbound_from_update: p2p chat → InboundMessage(channel=telegram, chat_type=p2p)"
    update, _b, _m = _fake_update(chat_type="private", user_id=7, chat_id=8, message_text="hi")
    inbound = inbound_from_update(update)
    ok = (
        inbound.channel == "telegram"
        and inbound.operator_id == "7"
        and inbound.chat_id == "8"
        and inbound.chat_type == "p2p"
        and inbound.text == "hi"
    )
    return CheckResult(name, ok, f"inbound={inbound!r}")


def _test_inbound_group() -> CheckResult:
    name = "inbound_from_update: group chat → chat_type='group'"
    update, _b, _m = _fake_update(chat_type="group", user_id=1, chat_id=2, message_text="")
    inbound = inbound_from_update(update)
    return CheckResult(name, inbound.chat_type == "group", f"chat_type={inbound.chat_type!r}")


def _test_send_text_returns_id() -> CheckResult:
    name = "send_text: returns sent message_id as str"
    update, _b, msg = _fake_update(send_returns=12345)
    out = asyncio.run(send_text(update, "yo"))
    return CheckResult(name, out == "12345" and len(msg.sent) == 1, f"out={out!r}")


def _test_send_text_no_message() -> CheckResult:
    name = "send_text: returns None when no effective_message"
    update, _b, _m = _fake_update()
    update.effective_message = None
    out = asyncio.run(send_text(update, "yo"))
    return CheckResult(name, out is None, f"out={out!r}")


def _test_edit_text_success() -> CheckResult:
    name = "edit_text: True on successful edit, calls bot.edit_message_text"
    update, bot, _m = _fake_update()
    out = asyncio.run(edit_text(update, 555, "new"))
    return CheckResult(name, out is True and bot.edits == [(2002, 555, "new")], f"out={out} edits={bot.edits}")


def _test_edit_text_not_modified() -> CheckResult:
    name = "edit_text: True on 'Message is not modified'"
    err = Exception("Message is not modified")
    update, bot, _m = _fake_update(edit_raises=err)
    out = asyncio.run(edit_text(update, 555, "new"))
    return CheckResult(name, out is True and bot.edits == [], f"out={out}")


def _test_edit_text_bad_id() -> CheckResult:
    name = "edit_text: False on bad placeholder_id"
    update, _b, _m = _fake_update()
    return CheckResult(name, asyncio.run(edit_text(update, "not-an-int", "x")) is False, "")


def _test_edit_text_missing_message() -> CheckResult:
    name = "edit_text: False when no effective_message"
    update, _b, _m = _fake_update()
    update.effective_message = None
    return CheckResult(name, asyncio.run(edit_text(update, 1, "x")) is False, "")


def _test_edit_text_other_failure() -> CheckResult:
    name = "edit_text: False on generic failure (BadRequest other)"
    err = Exception("Bad Request: message to edit not found")
    update, _b, _m = _fake_update(edit_raises=err)
    return CheckResult(name, asyncio.run(edit_text(update, 1, "x")) is False, "")


def _test_outbound_reply() -> CheckResult:
    name = "TelegramOutbound.reply: returns message_id"
    update, _b, msg = _fake_update(send_returns=77)
    port = TelegramOutbound(update)
    inbound = inbound_from_update(update)
    out = asyncio.run(port.reply(inbound, "hi"))
    return CheckResult(name, out == "77" and len(msg.sent) == 1, f"out={out!r}")


def _test_outbound_send_new() -> CheckResult:
    name = "TelegramOutbound.send_new: returns message_id"
    update, _b, msg = _fake_update(send_returns=88)
    port = TelegramOutbound(update)
    inbound = inbound_from_update(update)
    out = asyncio.run(port.send_new(inbound, "hi"))
    return CheckResult(name, out == "88" and len(msg.sent) == 1, f"out={out!r}")


def _test_outbound_edit_progress_success() -> CheckResult:
    name = "TelegramOutbound.edit_progress: True on success"
    update, _b, _m = _fake_update()
    port = TelegramOutbound(update)
    inbound = inbound_from_update(update)
    return CheckResult(name, asyncio.run(port.edit_progress(inbound, 9, "x")) is True, "")


def _test_outbound_edit_progress_not_modified() -> CheckResult:
    name = "TelegramOutbound.edit_progress: True on 'not modified'"
    err = Exception("BadRequest: Message is not modified")
    update, _b, _m = _fake_update(edit_raises=err)
    port = TelegramOutbound(update)
    inbound = inbound_from_update(update)
    return CheckResult(name, asyncio.run(port.edit_progress(inbound, 9, "x")) is True, "")


def _test_outbound_reply_with_buttons() -> CheckResult:
    name = "TelegramOutbound.reply_with_buttons: returns id and uses InlineKeyboardMarkup"
    update, _b, msg = _fake_update(send_returns=321)
    port = TelegramOutbound(update)
    inbound = inbound_from_update(update)
    buttons = [[
        {"text": "Confirm", "callback_data": "tool:confirm:abc"},
        {"text": "Cancel", "callback_data": "tool:cancel:abc"},
    ]]
    out = asyncio.run(port.reply_with_buttons(inbound, "Are you sure?", buttons))
    ok = out == "321" and len(msg.sent) == 1
    markup = msg.sent[0][1] if ok else None
    if ok and not isinstance(markup, InlineKeyboardMarkup):
        ok = False
    if ok and len(markup.inline_keyboard) != 1:
        ok = False
    if ok and [b.callback_data for b in markup.inline_keyboard[0]] != [
        "tool:confirm:abc", "tool:cancel:abc"
    ]:
        ok = False
    return CheckResult(name, ok, f"out={out!r} markup_type={type(markup).__name__}")


def _test_make_outbound() -> CheckResult:
    name = "make_outbound: returns TelegramOutbound bound to update"
    update, _b, _m = _fake_update()
    port = make_outbound(update)
    return CheckResult(
        name,
        isinstance(port, TelegramOutbound) and port._update is update,
        f"port={type(port).__name__}",
    )


def _test_inbound_explicit_text() -> CheckResult:
    name = "inbound_from_update: explicit text argument overrides message text"
    update, _b, _m = _fake_update(message_text="ignored", user_id=3, chat_id=4)
    inbound = inbound_from_update(update, text="/run rebuild")
    return CheckResult(name, inbound.text == "/run rebuild", f"text={inbound.text!r}")


def _test_inbound_no_message() -> CheckResult:
    name = "inbound_from_update: missing message → text='' message_id=None"
    update, _b, _m = _fake_update(message_text="ignored")
    update.effective_message = None
    inbound = inbound_from_update(update)
    return CheckResult(
        name,
        inbound.text == "" and inbound.message_id is None,
        f"text={inbound.text!r} mid={inbound.message_id!r}",
    )


def main() -> int:
    results = [
        _test_inbound_p2p(),
        _test_inbound_group(),
        _test_inbound_explicit_text(),
        _test_inbound_no_message(),
        _test_send_text_returns_id(),
        _test_send_text_no_message(),
        _test_edit_text_success(),
        _test_edit_text_not_modified(),
        _test_edit_text_bad_id(),
        _test_edit_text_missing_message(),
        _test_edit_text_other_failure(),
        _test_outbound_reply(),
        _test_outbound_send_new(),
        _test_outbound_edit_progress_success(),
        _test_outbound_edit_progress_not_modified(),
        _test_outbound_reply_with_buttons(),
        _test_make_outbound(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("channel telegram smoke ok" if ok else "channel telegram smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
