"""channel/feishu.py — Feishu-specific OutboundPort + adapter helpers.

P2.1: pulled out of feishu_bot.py so the entrypoint stays small.
Behavior must be byte-identical to the inlined version.

Public surface:
  - FeishuOutbound          — OutboundPort implementation
  - inbound_from_event      — FeishuChannel event message → InboundMessage

Allowed imports: `lark_oapi`, `channel.types`, `redaction.truncate`,
logging. MUST NOT import the Telegram SDK or `runner` / `handlers/*`.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from lark_oapi.channel import FeishuChannel

from channel.mentions import strip_mention
from channel.types import InboundMessage, ReplyContext
from redaction import truncate

logger = logging.getLogger("conveyor.channel.feishu")


# ---- OutboundPort ----------------------------------------------------------


def _make_card(text: str) -> dict:
    """Build a Feishu interactive card with markdown content.

    ``update_multi: true`` is required so later ``update_card`` PATCH
    calls can modify the card in-place (shared card visible to all).
    """
    return {
        "config": {"update_multi": True},
        "elements": [
            {
                "tag": "markdown",
                "content": truncate(text),
            }
        ],
    }


class FeishuOutbound:
    """OutboundPort backed by a FeishuChannel.

    Sends messages as interactive cards so edit_progress can update
    them in-place via the Feishu PATCH API (P2.2).  Falls back to
    plain text send_new if card send fails.
    """
    supports_inline_buttons: bool = False

    def __init__(self, channel: FeishuChannel) -> None:
        self._channel = channel

    async def reply(self, msg: InboundMessage, text: str):
        result = await self._send_card(msg, text, reply_to=msg.message_id)
        return result

    async def send_new(self, msg: InboundMessage, text: str):
        result = await self._send_card(msg, text, reply_to=None)
        return result

    async def edit_progress(
        self, msg: InboundMessage, placeholder_id: Any, text: str
    ) -> bool:
        if not placeholder_id:
            return False
        try:
            card = _make_card(text)
            result = await self._channel.update_card(str(placeholder_id), card)
            if result.ok:
                return True
            logger.debug(
                "Feishu edit_progress update_card failed: %s", result.error,
            )
            return False
        except Exception:
            logger.debug("Feishu edit_progress exception", exc_info=True)
            return False

    async def reply_with_buttons(
        self,
        msg: InboundMessage,
        text: str,
        buttons: Sequence[Sequence[dict]],
    ):
        result = await self._send_card(msg, text, reply_to=msg.message_id)
        return result

    async def send_card(
        self,
        msg: InboundMessage,
        card: dict,
        *,
        reply_to: str | None = None,
    ) -> str | None:
        """Send a pre-built interactive card (Feishu only).

        ``card`` should be a Feishu interactive message dict
        (see ``channel.feishu_cards``). On any error — API failure or
        exception during send — the adapter falls back to plain text
        with the card's title + first markdown block (truncated). The
        fallback path is best-effort and never raises.
        """
        chat_id = msg.chat_id
        if not chat_id:
            return None
        opts = {"reply_to": reply_to or msg.message_id} if (reply_to or msg.message_id) else None
        try:
            result = await self._channel.send(chat_id, card, opts)
            if result.ok and result.message_id:
                return result.message_id
            logger.debug(
                "Feishu card send failed, falling back to text: %s",
                getattr(result, "error", ""),
            )
        except Exception:
            logger.debug(
                "Feishu card send exception, falling back to text",
                exc_info=True,
            )
        # Fallback: flatten the card to a short text reply. Pull the
        # header title + first markdown content so the user still
        # sees something meaningful. The full structured UI is gone
        # in this branch, but the text preserves the headline.
        try:
            header = card.get("header") or {}
            title_obj = header.get("title") or {}
            title = title_obj.get("content", "") if isinstance(title_obj, dict) else ""
            body = ""
            for el in card.get("elements") or []:
                if isinstance(el, dict) and el.get("tag") == "markdown":
                    body = el.get("content", "")
                    break
            text = (title + "\n" + body).strip() if title else body
        except Exception:
            text = "(card render failed)"
        fallback = {"text": truncate(text or "(card render failed)")}
        try:
            await self._channel.send(chat_id, fallback, opts)
        except Exception:
            logger.debug("Feishu text fallback send failed", exc_info=True)
        return None

    async def send_image(
        self,
        chat_id: str,
        image_path: str,
        *,
        caption: str | None = None,
    ) -> None:
        """Upload and send an image resource to Feishu chat.

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
            "Feishu send_image: chat_id=%s... path=%s size=%d bytes caption=%s",
            (chat_id or "")[:8],
            image_path,
            file_size,
            caption,
        )
        try:
            from lark_oapi.channel import MediaSource
            from lark_oapi.channel.types import OutboundImage
            media_source = MediaSource(kind="file", path=image_path)
            image_key = await self._channel.upload_media(media_source, kind="image")
            logger.debug("Feishu send_image: uploaded image_key obtained")
            msg = OutboundImage(
                source=MediaSource(kind="key", key=image_key),
                caption=caption,
            )
            await self._channel.send(chat_id, msg)
            logger.debug("Feishu send_image: send completed successfully")
        except ImportError:
            # MediaSource/OutboundImage not available in this SDK version.
            # Fall back to plain image message dict if supported.
            logger.warning(
                "Feishu send_image: MediaSource/OutboundImage not available, "
                "falling back to image message dict"
            )
            try:
                import aiofiles
                async with aiofiles.open(image_path, "rb") as f:
                    image_bytes = await f.read()
                img_msg = {"image": image_bytes, "caption": caption}
                await self._channel.send(chat_id, img_msg)
            except Exception as fallback_exc:
                logger.exception(
                    "Feishu send_image fallback also failed: %s", fallback_exc
                )
                raise
        except Exception:
            logger.exception(
                "Failed to send Feishu image: chat_id=%s... size=%d",
                (chat_id or "")[:8],
                file_size,
            )
            raise

    async def _send_card(
        self, msg: InboundMessage, text: str, *, reply_to: str | None
    ) -> str | None:
        """Send an interactive card.  Returns message_id on success,
        None on failure (falls back to plain text)."""
        chat_id = msg.chat_id
        if not chat_id:
            return None
        try:
            card = _make_card(text)
            opts = {"reply_to": reply_to} if reply_to else None
            result = await self._channel.send(chat_id, card, opts)
            if result.ok and result.message_id:
                return result.message_id
            logger.debug(
                "Feishu card send failed, falling back to text: %s",
                result.error,
            )
        except Exception:
            logger.debug("Feishu card send exception, falling back to text", exc_info=True)
        # Fallback: plain text (no in-place update possible).
        opts = {"reply_to": reply_to} if reply_to else None
        await self._channel.send(chat_id, {"text": truncate(text)}, opts)
        return None


# ---- Inbound conversion ----------------------------------------------------


def _bot_mention_names(msg: Any, bot_open_id: str | None) -> list[str]:
    """Display names under which ``msg`` @mentions this bot."""
    if not bot_open_id:
        return []
    names: list[str] = []
    for m in getattr(msg, "mentions", None) or ():
        if getattr(m, "open_id", None) == bot_open_id and getattr(m, "name", None):
            names.append(str(m.name))
    return names


def _mentions_bot(msg: Any, bot_open_id: str | None) -> bool:
    if bool(getattr(msg, "mentioned_bot", False)):
        return True
    if not bot_open_id:
        return False
    return any(
        getattr(m, "open_id", None) == bot_open_id
        for m in getattr(msg, "mentions", None) or ()
    )


def inbound_from_event(
    msg: Any,
    *,
    bot_open_id: str | None = None,
    reply_to: ReplyContext | None = None,
) -> InboundMessage:
    """Convert a FeishuChannel message event into the channel-agnostic
    InboundMessage used by handlers.dispatch. Same attribute lookups and
    chat_type fallback to "unknown" as the historical _to_inbound.

    With ``bot_open_id`` (the bot's own identity) ``mentioned_bot`` is
    derived from the event's mention list — the SDK leaves its own
    ``mentioned_bot`` flag unset for message events — and the bot's
    ``@name`` is stripped from the text so "@bot /status" parses.
    """
    sender_id = getattr(msg, "sender_id", None) or ""
    chat_id = getattr(msg, "chat_id", None) or getattr(
        getattr(msg, "conversation", None), "chat_id", None
    )
    message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    chat_type = getattr(msg, "chat_type", None) or "unknown"
    text = (getattr(msg, "content_text", None) or "").strip()
    for name in _bot_mention_names(msg, bot_open_id):
        text = strip_mention(text, name)
    return InboundMessage(
        channel="feishu",
        operator_id=str(sender_id),
        chat_id=str(chat_id) if chat_id is not None else "",
        message_id=str(message_id) if message_id is not None else None,
        text=text,
        chat_type=(
            chat_type if chat_type in ("p2p", "group", "unknown") else "unknown"
        ),
        mentioned_bot=_mentions_bot(msg, bot_open_id),
        reply_to=reply_to,
        raw=msg,
    )


def _post_text(content: dict) -> str:
    """Flatten Feishu ``post`` content (optionally locale-wrapped)."""
    if "content" not in content:
        for value in content.values():
            if isinstance(value, dict) and "content" in value:
                content = value
                break
    lines: list[str] = []
    title = content.get("title")
    if title:
        lines.append(str(title))
    for para in content.get("content") or []:
        parts: list[str] = []
        for el in para if isinstance(para, list) else []:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag in ("text", "a", "code_block", "md"):
                parts.append(str(el.get("text") or ""))
            elif tag == "at":
                parts.append("@" + str(el.get("user_name") or el.get("user_id") or ""))
        lines.append("".join(parts))
    return "\n".join(line for line in lines if line).strip()


def reply_context_from_payload(
    payload: Any, *, bot_app_id: str | None = None,
) -> ReplyContext | None:
    """Build a ReplyContext from a GET /im/v1/messages/:id response dict.

    Only text and post messages carry quotable text; other types (images,
    files, cards) return None so the request is handled without context.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    item = items[0]
    body = item.get("body") or {}
    raw_content = body.get("content") if isinstance(body, dict) else None
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except ValueError:
        return None
    if not isinstance(content, dict):
        return None
    msg_type = item.get("msg_type")
    if msg_type == "text":
        text = str(content.get("text") or "")
    elif msg_type == "post":
        text = _post_text(content)
    else:
        return None
    for m in item.get("mentions") or []:
        if isinstance(m, dict) and m.get("key"):
            text = text.replace(str(m["key"]), "@" + str(m.get("name") or ""))
    text = text.strip()
    if not text:
        return None
    sender = item.get("sender") or {}
    from_bot = bool(
        bot_app_id
        and sender.get("sender_type") == "app"
        and sender.get("id") == bot_app_id
    )
    return ReplyContext(text=text, from_bot=from_bot)


async def fetch_reply_context(
    channel: Any, msg: Any, *, bot_app_id: str | None = None,
) -> ReplyContext | None:
    """Fetch the message ``msg`` replies to (best-effort, never raises)."""
    reply = getattr(msg, "reply", None)
    parent_id = getattr(reply, "message_id", None) if reply is not None else None
    if not parent_id:
        return None
    inline_text = (getattr(reply, "text", None) or "").strip()
    if inline_text:
        return ReplyContext(text=inline_text)
    try:
        payload = await channel.driver.fetch_message(str(parent_id))
    except Exception:
        logger.debug("Feishu reply fetch failed for %s", parent_id, exc_info=True)
        return None
    return reply_context_from_payload(payload, bot_app_id=bot_app_id)


# Keep the historic `_to_inbound` name as a private alias so the
# inlined-onboarding / bootstrap call sites that may still reference it
# (e.g. in older test scripts) do not break.
_to_inbound = inbound_from_event
