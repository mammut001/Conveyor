#!/usr/bin/env python3
"""telegram_outbound_smoke.py — TelegramOutbound really edits
placeholders in place and falls back to send_new when edit fails.

P2.1: tests target `channel.telegram.TelegramOutbound` / `send_text` /
`edit_text`, not the old inlined `bot._TelegramOutbound`.

Pins:
  - AST: TelegramOutbound declares reply / send_new / edit_progress /
    reply_with_buttons as async.
  - AST: channel/telegram.py defines send_text and edit_text helpers
    used by TelegramOutbound.
  - behavior: with a fake bot + fake chat/message, reply() returns
    the sent message_id (as str).
  - behavior: edit_progress() invokes bot.edit_message_text with the
    chat.id and the int-casted placeholder_id; returns True on
    success.
  - behavior: when edit_message_text raises an error whose message
    contains "not modified", edit_progress returns True (no-op
    success).
  - behavior: when edit_message_text raises any other exception,
    edit_progress returns False.
  - behavior: when placeholder_id is not int-castable, edit_progress
    returns False without calling the bot.

Run: .venv/bin/python scripts/telegram_outbound_smoke.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel import InboundMessage
from channel.telegram import TelegramOutbound, edit_text, send_text
from scripts.harness_common import CheckResult, print_results


CHANNEL_PY = Path(__file__).resolve().parents[1] / "channel" / "telegram.py"


# ---- Fake SDK objects ----------------------------------------------------


class _FakeSentMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeChat:
    def __init__(self, chat_id: int = 7001) -> None:
        self.id = chat_id


class _FakeMessage:
    """Stand-in for telegram.Message; only the methods send_text uses."""

    def __init__(self, chat: _FakeChat, sent_id: int = 9001) -> None:
        self._chat = chat
        self._sent_id = sent_id
        self.reply_calls: list[dict] = []

    async def reply_text(self, text, **kwargs):
        self.reply_calls.append({"text": text, **kwargs})
        return _FakeSentMessage(self._sent_id)


class _FakeBot:
    def __init__(self) -> None:
        self.edit_calls: list[dict] = []
        # default: edit succeeds
        self.edit_succeeds: bool = True
        self.edit_exception: Exception | None = None

    async def edit_message_text(self, **kwargs):
        self.edit_calls.append(kwargs)
        if self.edit_exception is not None:
            raise self.edit_exception
        if not self.edit_succeeds:
            raise RuntimeError("network blip")
        return kwargs


def _make_update(chat: _FakeChat, message: _FakeMessage, bot_obj: _FakeBot):
    update = SimpleNamespace()
    update.effective_message = message
    update.effective_chat = chat
    update.effective_user = SimpleNamespace(id=12345, username="op")

    def get_bot():
        return bot_obj
    update.get_bot = get_bot
    return update


# ---- AST tests -----------------------------------------------------------


def _parse_channel() -> ast.Module:
    return ast.parse(CHANNEL_PY.read_text(encoding="utf-8"))


def _test_outbound_port_methods() -> CheckResult:
    name = "AST: TelegramOutbound declares reply/send_new/edit_progress as async"
    try:
        tree = _parse_channel()
        cls = next(
            (n for n in tree.body
             if isinstance(n, ast.ClassDef) and n.name == "TelegramOutbound"),
            None,
        )
        if cls is None:
            return CheckResult(name, False, "class missing")
        methods = {}
        for stmt in cls.body:
            if isinstance(stmt, (ast.AsyncFunctionDef, ast.FunctionDef)) and stmt.name in {
                "reply", "send_new", "edit_progress", "reply_with_buttons",
            }:
                methods[stmt.name] = isinstance(stmt, ast.AsyncFunctionDef)
        missing = {"reply", "send_new", "edit_progress", "reply_with_buttons"} - methods.keys()
        if missing:
            return CheckResult(name, False, f"missing methods: {sorted(missing)}")
        if not all(methods.values()):
            return CheckResult(name, False, f"not all async: {methods}")
        return CheckResult(name, True, f"all 4 are async: {sorted(methods)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_helpers_present() -> CheckResult:
    name = "AST: channel/telegram.py defines send_text and edit_text helpers"
    try:
        tree = _parse_channel()
        names = {n.name for n in tree.body
                 if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
        needed = {"send_text", "edit_text"}
        missing = needed - names
        if missing:
            return CheckResult(name, False, f"missing: {sorted(missing)}")
        return CheckResult(name, True, "send_text and edit_text defined")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavior tests ------------------------------------------------------


def _test_reply_returns_message_id() -> CheckResult:
    name = "behavior: TelegramOutbound.reply() returns the sent message_id as str"
    try:
        chat = _FakeChat()
        message = _FakeMessage(chat, sent_id=42)
        bot_obj = _FakeBot()
        update = _make_update(chat, message, bot_obj)
        port = TelegramOutbound(update)
        msg = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id=str(chat.id),
            message_id=None,
            text="",
        )
        result = asyncio.run(port.reply(msg, "hello"))
        if result != "42":
            return CheckResult(name, False, f"expected '42', got {result!r}")
        if not message.reply_calls:
            return CheckResult(name, False, "reply_text was not called")
        return CheckResult(name, True, f"reply_text called once, returned {result!r}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_send_text_helper() -> CheckResult:
    name = "behavior: channel.telegram.send_text() returns the sent message_id as str"
    try:
        chat = _FakeChat()
        message = _FakeMessage(chat, sent_id=43)
        bot_obj = _FakeBot()
        update = _make_update(chat, message, bot_obj)
        out = asyncio.run(send_text(update, "hi"))
        return CheckResult(name, out == "43", f"out={out!r}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_progress_succeeds() -> CheckResult:
    name = "behavior: edit_progress() invokes bot.edit_message_text with int(placeholder_id), returns True"
    try:
        chat = _FakeChat(chat_id=8001)
        message = _FakeMessage(chat)
        bot_obj = _FakeBot()
        update = _make_update(chat, message, bot_obj)
        port = TelegramOutbound(update)
        msg = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id=str(chat.id),
            message_id=None,
            text="",
        )
        result = asyncio.run(port.edit_progress(msg, "555", "new body"))
        if result is not True:
            return CheckResult(name, False, f"expected True, got {result!r}")
        if len(bot_obj.edit_calls) != 1:
            return CheckResult(name, False, f"expected 1 edit call, got {len(bot_obj.edit_calls)}")
        call = bot_obj.edit_calls[0]
        if call.get("chat_id") != 8001:
            return CheckResult(name, False, f"chat_id wrong: {call!r}")
        if call.get("message_id") != 555:
            return CheckResult(name, False, f"message_id wrong: {call!r}")
        return CheckResult(
            name, True,
            f"edit succeeded, args={ {k: v for k, v in call.items() if k in ('chat_id','message_id')} }",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_progress_not_modified_is_success() -> CheckResult:
    name = "behavior: edit_progress() treats 'Message is not modified' as success (returns True)"
    try:
        chat = _FakeChat(chat_id=8002)
        message = _FakeMessage(chat)
        bot_obj = _FakeBot()
        bot_obj.edit_exception = _BadRequestLike("Message is not modified")
        update = _make_update(chat, message, bot_obj)
        port = TelegramOutbound(update)
        msg = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id=str(chat.id),
            message_id=None,
            text="",
        )
        result = asyncio.run(port.edit_progress(msg, "42", "same text"))
        if result is not True:
            return CheckResult(name, False, f"expected True (no-op), got {result!r}")
        return CheckResult(name, True, "BadRequest(not modified) mapped to True")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_progress_other_error_falls_back() -> CheckResult:
    name = "behavior: edit_progress() returns False when bot raises other exception"
    try:
        chat = _FakeChat(chat_id=8003)
        message = _FakeMessage(chat)
        bot_obj = _FakeBot()
        bot_obj.edit_succeeds = False  # raises RuntimeError("network blip")
        update = _make_update(chat, message, bot_obj)
        port = TelegramOutbound(update)
        msg = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id=str(chat.id),
            message_id=None,
            text="",
        )
        result = asyncio.run(port.edit_progress(msg, "42", "new"))
        if result is not False:
            return CheckResult(name, False, f"expected False, got {result!r}")
        return CheckResult(name, True, "non-not-modified error → False (caller falls back to send_new)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_progress_bad_placeholder_id() -> CheckResult:
    name = "behavior: edit_progress() returns False on non-int placeholder_id"
    try:
        chat = _FakeChat(chat_id=8004)
        message = _FakeMessage(chat)
        bot_obj = _FakeBot()
        update = _make_update(chat, message, bot_obj)
        port = TelegramOutbound(update)
        msg = InboundMessage(
            channel="telegram",
            operator_id="12345",
            chat_id=str(chat.id),
            message_id=None,
            text="",
        )
        result = asyncio.run(port.edit_progress(msg, "not-an-int", "x"))
        if result is not False:
            return CheckResult(name, False, f"expected False, got {result!r}")
        if bot_obj.edit_calls:
            return CheckResult(name, False, "bot.edit_message_text was called for bad id")
        return CheckResult(name, True, "bad placeholder_id short-circuits to False")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_text_helper_returns_bool() -> CheckResult:
    name = "behavior: channel.telegram.edit_text() returns False on bad id"
    try:
        chat = _FakeChat()
        message = _FakeMessage(chat)
        bot_obj = _FakeBot()
        update = _make_update(chat, message, bot_obj)
        out = asyncio.run(edit_text(update, "nope", "x"))
        return CheckResult(name, out is False, f"out={out!r}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# python-telegram-bot's BadRequest isn't available without a real bot
# import; we synthesize a stand-in class so the smoke is hermetic.
class _BadRequestLike(Exception):
    pass


CHECKS = [
    _test_outbound_port_methods,
    _test_helpers_present,
    _test_reply_returns_message_id,
    _test_send_text_helper,
    _test_edit_progress_succeeds,
    _test_edit_progress_not_modified_is_success,
    _test_edit_progress_other_error_falls_back,
    _test_edit_progress_bad_placeholder_id,
    _test_edit_text_helper_returns_bool,
]


def main() -> int:
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("telegram_outbound smoke ok" if ok else "telegram_outbound smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
