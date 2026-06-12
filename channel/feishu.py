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

import logging
from typing import Any, Sequence

from lark_oapi.channel import FeishuChannel

from channel.types import InboundMessage
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


def inbound_from_event(msg: Any) -> InboundMessage:
    """Convert a FeishuChannel message event into the channel-agnostic
    InboundMessage used by handlers.dispatch. Preserves the historical
    _to_inbound behavior exactly: same attribute lookups, same chat_type
    fallback to "unknown", same `mentioned_bot` flag.
    """
    sender_id = getattr(msg, "sender_id", None) or ""
    chat_id = getattr(msg, "chat_id", None) or getattr(
        getattr(msg, "conversation", None), "chat_id", None
    )
    message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    chat_type = getattr(msg, "chat_type", None) or "unknown"
    text = (getattr(msg, "content_text", None) or "").strip()
    return InboundMessage(
        channel="feishu",
        operator_id=str(sender_id),
        chat_id=str(chat_id) if chat_id is not None else "",
        message_id=str(message_id) if message_id is not None else None,
        text=text,
        chat_type=(
            chat_type if chat_type in ("p2p", "group", "unknown") else "unknown"
        ),
        mentioned_bot=bool(getattr(msg, "mentioned_bot", False)),
        raw=msg,
    )


# Keep the historic `_to_inbound` name as a private alias so the
# inlined-onboarding / bootstrap call sites that may still reference it
# (e.g. in older test scripts) do not break.
_to_inbound = inbound_from_event
