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

from channel.mentions import mentions, strip_mention
from channel.types import Attachment, InboundMessage, OutboundPort, ReplyContext
from redaction import truncate

logger = logging.getLogger("conveyor.channel.telegram")

# Telegram reports a dead forum topic (closed / deleted) in several
# phrasings depending on the API version; all of them mean the topic can
# no longer receive messages.
_DEAD_TOPIC_MARKERS = (
    "message thread not found",
    "topic_closed",
    "topic_deleted",
    "topic closed",
    "topic deleted",
    "thread not found",
)

# Prepended when a topic-scoped send falls back to the base chat so the
# reader knows why the message arrived outside the topic.
_DEAD_TOPIC_FALLBACK_PREFIX = "（原话题已关闭或删除，改发到群聊）\n"


def _is_dead_topic_error(exc: BaseException) -> bool:
    """True when Telegram says the forum topic is closed/deleted.

    Sending to ``<chat>:topic:<thread>`` fails with ``BadRequest`` once
    the topic is gone; callers use this to fall back to the base chat
    instead of silently dropping the message.
    """
    try:
        text = str(exc).lower()
    except Exception:
        return False
    return any(marker in text for marker in _DEAD_TOPIC_MARKERS)


# ---- Inbound conversion ----------------------------------------------------


def _bot_identity(update: Update) -> tuple[str, int | None]:
    """(username, id) of this bot, or ("", None) when unavailable
    (fake updates in tests, or a bot that has not been initialized)."""
    try:
        bot = update.get_bot()
        username = getattr(bot, "username", "")
        bot_id = getattr(bot, "id", None)
    except Exception:
        return ("", None)
    return (
        username if isinstance(username, str) else "",
        bot_id if isinstance(bot_id, int) else None,
    )


def _display_name(user: Any) -> str:
    if user is None:
        return ""
    full = getattr(user, "full_name", None)
    if full:
        return str(full)
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or str(getattr(user, "username", "") or "")


def _is_topic_root(msg: Any, reply: Any) -> bool:
    """Forum topics make every message a reply to the topic's service
    message; that is not a real reply and must not become context."""
    if getattr(reply, "forum_topic_created", None) is not None:
        return True
    return bool(
        getattr(msg, "is_topic_message", False)
        and getattr(reply, "message_id", None) is not None
        and getattr(reply, "message_id", None) == getattr(msg, "message_thread_id", None)
    )


def _reply_context(msg: Any, bot_id: int | None) -> ReplyContext | None:
    """Replied-to / quoted message as ReplyContext.

    A partial quote (the user selected part of the message) wins over the
    whole replied-to text; a quote of a message in another chat
    (``external_reply``) is used when there is no in-chat reply.
    """
    if msg is None:
        return None
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None and _is_topic_root(msg, reply):
        reply = None
    quote = getattr(msg, "quote", None)
    quote_text = (getattr(quote, "text", None) or "").strip() if quote else ""
    if reply is None:
        if quote_text and getattr(msg, "external_reply", None) is not None:
            return ReplyContext(text=quote_text, partial_quote=True)
        return None
    text = quote_text or (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
    if not text:
        return None
    user = getattr(reply, "from_user", None)
    author = _display_name(user)
    if not author:
        author = str(getattr(getattr(reply, "sender_chat", None), "title", "") or "")
    from_bot = bot_id is not None and getattr(user, "id", None) == bot_id
    return ReplyContext(
        text=text,
        author=author,
        from_bot=from_bot,
        partial_quote=bool(quote_text),
    )


# Telegram's bot API only serves files up to 20 MB; the store caps at 10 MB.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _image_of(message: Any, origin: str) -> Attachment | None:
    """The best image in ``message`` (largest photo size under the cap,
    or an image document), as an Attachment reference."""
    if message is None:
        return None
    photos = list(getattr(message, "photo", None) or ())
    if photos:
        fitting = [p for p in photos if (getattr(p, "file_size", 0) or 0) <= _MAX_IMAGE_BYTES]
        best = (fitting or photos)[-1]
        return Attachment(
            kind="image", ref=str(best.file_id), origin=origin,
            size=getattr(best, "file_size", None),
        )
    doc = getattr(message, "document", None)
    mime = str(getattr(doc, "mime_type", "") or "") if doc is not None else ""
    if doc is not None and mime.startswith("image/"):
        return Attachment(
            kind="image", ref=str(doc.file_id), origin=origin,
            size=getattr(doc, "file_size", None),
        )
    return None


def _attachments(msg: Any) -> tuple[Attachment, ...]:
    """Images on the message itself, then on the message it replies to."""
    if msg is None:
        return ()
    found: list[Attachment] = []
    own = _image_of(msg, "message")
    if own is not None:
        found.append(own)
    reply = getattr(msg, "reply_to_message", None)
    if reply is not None and not _is_topic_root(msg, reply):
        replied = _image_of(reply, "reply")
        if replied is not None:
            found.append(replied)
    return tuple(found)


def _replies_to_bot(msg: Any, bot_id: int | None) -> bool:
    reply = getattr(msg, "reply_to_message", None) if msg is not None else None
    if reply is None or bot_id is None or _is_topic_root(msg, reply):
        return False
    return getattr(getattr(reply, "from_user", None), "id", None) == bot_id


def _mentions_bot(msg: Any, text: str, username: str, bot_id: int | None) -> bool:
    if username and mentions(text, username):
        return True
    entities = tuple(getattr(msg, "entities", None) or ()) + tuple(
        getattr(msg, "caption_entities", None) or ()
    )
    for entity in entities:
        if getattr(entity, "type", None) == "text_mention":
            user = getattr(entity, "user", None)
            if bot_id is not None and getattr(user, "id", None) == bot_id:
                return True
    return False


def _thread_id(msg: Any, chat: Any = None) -> str | None:
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if isinstance(tid, int) and tid > 0 and (
        getattr(msg, "is_topic_message", False)
        or getattr(chat, "is_forum", False)
        or getattr(getattr(msg, "chat", None), "is_forum", False)
    ):
        return str(tid)
    return None


def split_chat_thread(chat_id: str | int) -> tuple[int | str, int | None]:
    """Parse `<chat_id>:topic:<thread_id>` or plain `<chat_id>` into `(chat_id, thread_id)`."""
    raw = str(chat_id or "")
    if ":topic:" in raw:
        base, _, tid = raw.partition(":topic:")
        try:
            return int(base), int(tid)
        except ValueError:
            return base, None
    try:
        return int(raw), None
    except ValueError:
        return raw, None


def inbound_from_update(
    update: Update, text: str | None = None
) -> InboundMessage:
    """Convert a python-telegram-bot Update into the channel-agnostic
    InboundMessage used by handlers.dispatch.
      * `text` argument wins over the update's message text
      * missing message → `text=""` and `message_id=None`
      * chat type is "p2p" for private chats, otherwise "group"
      * `mentioned_bot` is True when the text @mentions this bot (or
        text-mentions it) or the message replies to one of its messages;
        the bot's own @username is stripped from `text`
      * `reply_to` carries the replied-to / quoted message text
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    text_value = text
    if text_value is None and msg is not None:
        text_value = msg.text or getattr(msg, "caption", None) or ""
    text_value = text_value or ""
    username, bot_id = _bot_identity(update)
    reply_to = _reply_context(msg, bot_id)
    mentioned = _mentions_bot(msg, text_value, username, bot_id) or _replies_to_bot(msg, bot_id)
    if username:
        text_value = strip_mention(text_value, username)
    return InboundMessage(
        channel="telegram",
        operator_id=str(getattr(user, "id", "") or ""),
        chat_id=str(getattr(chat, "id", "") or ""),
        message_id=(str(getattr(msg, "message_id", "") or "")
                    if msg is not None else None),
        text=text_value.strip(),
        chat_type=("p2p" if (chat and getattr(chat, "type", None) == "private")
                   else "group"),
        mentioned_bot=mentioned,
        reply_to=reply_to,
        attachments=_attachments(msg),
        raw=update,
        thread_id=_thread_id(msg, chat),
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
    supports_attachments: bool = True

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

    async def send_card(
        self,
        msg: InboundMessage,
        card: dict,
        *,
        reply_to: str | None = None,
    ) -> str | None:
        """Telegram has no native card type: flatten the card to text
        and send via ``reply``.

        This keeps the handler-level interface symmetric with Feishu
        (which sends the actual card) while leaving Telegram behavior
        unchanged. If you want the card content verbatim in Telegram,
        see ``channel.feishu_cards.flatten_card_to_text``.
        """
        from channel.feishu_cards import flatten_card_to_text  # lazy
        text = flatten_card_to_text(card)
        return await send_text(self._update, text)

    async def fetch_attachment(
        self, msg: InboundMessage, attachment: Attachment,
    ) -> bytes | None:
        """Download a Telegram file by file_id (size-capped)."""
        if attachment.size is not None and attachment.size > _MAX_IMAGE_BYTES:
            return None
        bot = self._update.get_bot()
        tg_file = await bot.get_file(attachment.ref)
        size = getattr(tg_file, "file_size", None)
        if size is not None and size > _MAX_IMAGE_BYTES:
            return None
        data = await tg_file.download_as_bytearray()
        return bytes(data) if len(data) <= _MAX_IMAGE_BYTES else None

    async def send_image(
        self,
        chat_id: str,
        image_path: str,
        *,
        caption: str | None = None,
    ) -> None:
        """Send a photo/image file on Telegram.

        Raises on any failure so callers can mark delivery_failed instead of
        falsely marking the request as delivered.
        """
        import os
        file_size = 0
        try:
            file_size = os.path.getsize(image_path)
        except OSError:
            pass
        logger.debug(
            "Telegram send_image: chat_id=%s... path=%s size=%d bytes",
            (chat_id or "")[:8],
            image_path,
            file_size,
        )
        bot = self._update.get_bot()
        target_chat, thread_id = split_chat_thread(chat_id)
        photo_kwargs: dict[str, Any] = {}
        if thread_id is not None:
            photo_kwargs["message_thread_id"] = thread_id
        try:
            with open(image_path, "rb") as f:
                await bot.send_photo(
                    chat_id=target_chat,
                    photo=f,
                    caption=caption,
                    **photo_kwargs,
                )
            logger.debug("Telegram send_image: send completed successfully")
        except Exception:
            logger.exception(
                "Failed to send Telegram photo: chat_id=%s... size=%d",
                (chat_id or "")[:8],
                file_size,
            )
            raise


def make_outbound(update: Update) -> TelegramOutbound:
    return TelegramOutbound(update)


class TelegramChatOutbound(OutboundPort):
    supports_inline_buttons: bool = True
    supports_attachments: bool = False

    def __init__(self, bot: Any, chat_id: str | int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        return await self.send_new(msg, text)

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        target_chat, thread_id = split_chat_thread(self.chat_id)
        extra: dict[str, Any] = {}
        if thread_id is not None:
            extra["message_thread_id"] = thread_id
        try:
            sent = await self.bot.send_message(
                chat_id=target_chat,
                text=truncate(text),
                disable_web_page_preview=True,
                **extra,
            )
            return str(getattr(sent, "message_id", "") or "")
        except Exception as exc:
            if thread_id is not None and _is_dead_topic_error(exc):
                return await self._resend_to_base_chat(text, reply_markup=None)
            logger.exception("Failed to send Telegram message from TelegramChatOutbound")
            return None

    async def _resend_to_base_chat(
        self, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> str | None:
        """Retry a topic send in the base chat without message_thread_id.

        Used when the forum topic is closed or deleted, so goal-loop
        notices / approval relays / agent results are not lost. The
        text is prefixed to explain the redirection.
        """
        target_chat, _ = split_chat_thread(self.chat_id)
        logger.warning(
            "Telegram topic is closed/deleted; resending to base chat %s...",
            str(target_chat)[:8],
        )
        try:
            sent = await self.bot.send_message(
                chat_id=target_chat,
                text=truncate(_DEAD_TOPIC_FALLBACK_PREFIX + text),
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
            return str(getattr(sent, "message_id", "") or "")
        except Exception:
            logger.exception("Telegram base-chat fallback send failed too")
            return None

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        return True

    async def reply_with_buttons(
        self,
        msg: InboundMessage,
        text: str,
        buttons: Sequence[Sequence[dict]],
    ) -> str | None:
        target_chat, thread_id = split_chat_thread(self.chat_id)
        extra: dict[str, Any] = {}
        if thread_id is not None:
            extra["message_thread_id"] = thread_id
        try:
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
                for row in buttons
            ]
            sent = await self.bot.send_message(
                chat_id=target_chat,
                text=truncate(text),
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
                **extra,
            )
            return str(getattr(sent, "message_id", "") or "")
        except Exception as exc:
            if thread_id is not None and _is_dead_topic_error(exc):
                return await self._resend_to_base_chat(
                    text, reply_markup=InlineKeyboardMarkup(keyboard)
                )
            logger.exception("Failed to send Telegram message with buttons")
            return None


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
        if name == "RetryAfter" or "flood control" in msg.lower() or "too many requests" in msg.lower():
            # Flood control on a progress edit only means this frame came too
            # fast. Skip it; reporting failure latches the caller into its
            # fallback and silences progress for the rest of the job (fast
            # Pi tool loops hit this).
            logger.debug("edit_progress throttled by Telegram: %s", msg)
            return True
        logger.debug(
            "edit_progress failed (%s): %s; will fall back to send_new",
            name, msg,
        )
        return False
