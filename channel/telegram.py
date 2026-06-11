"""channel/telegram.py — Telegram-specific OutboundPort + adapter helpers.

P2.1: pulled out of bot.py so the entrypoint stays small. Behavior
must be byte-identical to the inlined versions in bot.py.

Public surface:
  - TelegramOutbound           — OutboundPort implementation
  - inbound_from_update        — Update → InboundMessage
  - make_outbound              — Update → TelegramOutbound

Allowed imports: `telegram` SDK, `channel.types`, `redaction.truncate`,
logging. MUST NOT import `runner` or any `handlers/*` business logic.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from channel.types import InboundMessage
from redaction import truncate

logger = logging.getLogger("conveyor.channel.telegram")


# ---- Inbound conversion ----------------------------------------------------


def inbound_from_update(
    update: Update, text: str | None = None
) -> InboundMessage:
    """Convert a python-telegram-bot Update into the channel-agnostic
    InboundMessage used by handlers.dispatch. Preserves the historical
    bot.py behavior exactly:
      * `text` argument wins over the update's message text
      * missing message → `text=""` and `message_id=None`
      * chat type is "p2p" for private chats, otherwise "group"
        (no explicit "unknown" — matches pre-P2.1 behavior)
      * `mentioned_bot` is left at the dataclass default (False)
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    text_value = text
    if text_value is None and msg is not None:
        text_value = msg.text or ""
    return InboundMessage(
        channel="telegram",
        operator_id=str(getattr(user, "id", "") or ""),
        chat_id=str(getattr(chat, "id", "") or ""),
        message_id=(str(getattr(msg, "message_id", "") or "")
                    if msg is not None else None),
        text=(text_value or "").strip(),
        chat_type=("p2p" if (chat and getattr(chat, "type", None) == "private")
                   else "group"),
        raw=update,
    )


# ---- OutboundPort ----------------------------------------------------------


class TelegramOutbound:
    """Telegram OutboundPort: real edit-in-place progress.

    `reply()` and `send_new()` return the sent message_id as a str so
    handlers can hand it to `edit_progress` for in-place edits. The
    latch on the first edit failure lives in handlers/jobs.py, not
    here.
    """
    supports_inline_buttons: bool = True

    def __init__(self, update: Update) -> None:
        self._update = update

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        return await send_text(self._update, text)

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        return await send_text(self._update, text)

    async def edit_progress(
        self, msg: InboundMessage, placeholder_id: Any, text: str
    ) -> bool:
        return await edit_text(self._update, placeholder_id, text)

    async def reply_with_buttons(
        self,
        msg: InboundMessage,
        text: str,
        buttons: Sequence[Sequence[dict]],
    ):
        keyboard = [
            [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
             for b in row]
            for row in buttons
        ]
        return await send_text(
            self._update, text, reply_markup=InlineKeyboardMarkup(keyboard)
        )


def make_outbound(update: Update) -> TelegramOutbound:
    return TelegramOutbound(update)


# ---- Low-level send/edit helpers ------------------------------------------


async def send_text(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> str | None:
    """Send a message; return the sent message_id as str|None.

    Returns the id so OutboundPort.reply/send_new can hand it to
    edit_progress for in-place Telegram updates. Returns None on any
    failure (logs and continues) so the dispatcher doesn't crash on
    a transient network blip.
    """
    message = update.effective_message
    if message is None:
        return None
    try:
        sent = await message.reply_text(
            truncate(text),
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Failed to send Telegram message")
        return None
    return str(getattr(sent, "message_id", "") or "") or None


async def edit_text(
    update: Update, placeholder_id: Any, text: str
) -> bool:
    """Edit an existing Telegram message in place. Returns True on
    success, False on any failure (handler falls back to send_new).

    Catches "Message is not modified" (Telegram 400) and treats it as
    success — the wire content is already what we wanted, and short-
    circuiting the fallback keeps progress text stable.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not placeholder_id:
        return False
    try:
        pid_int = int(placeholder_id)
    except (TypeError, ValueError):
        return False
    try:
        await update.get_bot().edit_message_text(
            chat_id=chat.id,
            message_id=pid_int,
            text=truncate(text),
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        name = exc.__class__.__name__
        msg = str(exc)
        if "not modified" in msg.lower():
            return True
        logger.debug(
            "edit_progress failed (%s): %s; will fall back to send_new",
            name, msg,
        )
        return False
