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


class FeishuOutbound:
    """OutboundPort backed by a FeishuChannel.

    First version: every reply is a fresh message. The OutboundPort
    contract promises edit_progress, so we accept the placeholder id
    but degrade to send_new — this matches the prior feishu_bot.py
    behavior (no streaming cards) and is enough for /status, /diff,
    Codex replies.
    """
    supports_inline_buttons: bool = False

    def __init__(self, channel: FeishuChannel) -> None:
        self._channel = channel

    async def reply(self, msg: InboundMessage, text: str):
        await self._send(msg, text, reply_to=msg.message_id)
        return None

    async def send_new(self, msg: InboundMessage, text: str):
        await self._send(msg, text, reply_to=None)
        return None

    async def edit_progress(
        self, msg: InboundMessage, placeholder_id: Any, text: str
    ) -> bool:
        # Feishu OutboundPort does not implement streaming edit; let
        # the caller fall back to send_new on the first latched edit.
        return False

    async def reply_with_buttons(
        self,
        msg: InboundMessage,
        text: str,
        buttons: Sequence[Sequence[dict]],
    ):
        # Feishu cards/interactive messages are not yet implemented; the
        # safe fall-back is to send a text representation. The caller
        # may append a textual button hint per row from `buttons` later
        # if needed (P2.2 backlog).
        await self._send(msg, text, reply_to=msg.message_id)
        return None

    async def _send(
        self, msg: InboundMessage, text: str, *, reply_to: str | None
    ) -> None:
        chat_id = msg.chat_id
        if not chat_id:
            return
        opts = {"reply_to": reply_to} if reply_to else None
        await self._channel.send(chat_id, {"text": truncate(text)}, opts)


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
