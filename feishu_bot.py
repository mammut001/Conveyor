#!/usr/bin/env python3
"""Feishu/Lark bot — WebSocket long connection + handlers/dispatch.

Run locally while configuring the Feishu developer console (Events must see
an online long-connection client before saving im.message.receive_v1).

  export LARK_APP_ID=cli_xxx
  export LARK_APP_SECRET=xxx
  # optional until first message: LARK_ALLOWED_OPEN_ID=ou_xxx
  .venv/bin/python feishu_bot.py
"""
from __future__ import annotations

import asyncio
import logging

from lark_oapi.channel import FeishuChannel

from channel import InboundMessage
from channel.auth import is_allowed
from config import load_feishu_settings
from handlers import dispatch
from redaction import redact_text, truncate
from runner import CodexRunner

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codex_feishu_bot")

settings = load_feishu_settings()
runner = CodexRunner(settings)


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

    async def edit_progress(self, msg: InboundMessage, placeholder_id, text: str) -> bool:
        # Feishu OutboundPort does not implement streaming edit; let
        # the caller fall back to send_new on the first latched edit.
        return False

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons):
        await self._send(msg, text, reply_to=msg.message_id)
        return None

    async def _send(self, msg: InboundMessage, text: str, *, reply_to: str | None) -> None:
        chat_id = msg.chat_id
        if not chat_id:
            return
        opts = {"reply_to": reply_to} if reply_to else None
        await self._channel.send(chat_id, {"text": truncate(text)}, opts)


def _to_inbound(msg) -> InboundMessage:
    sender_id = getattr(msg, "sender_id", None) or ""
    chat_id = getattr(msg, "chat_id", None) or getattr(getattr(msg, "conversation", None), "chat_id", None)
    message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    chat_type = getattr(msg, "chat_type", None) or "unknown"
    text = (getattr(msg, "content_text", None) or "").strip()
    return InboundMessage(
        channel="feishu",
        operator_id=str(sender_id),
        chat_id=str(chat_id) if chat_id is not None else "",
        message_id=str(message_id) if message_id is not None else None,
        text=text,
        chat_type=chat_type if chat_type in ("p2p", "group", "unknown") else "unknown",
        mentioned_bot=bool(getattr(msg, "mentioned_bot", False)),
        raw=msg,
    )


async def main() -> None:
    if not settings.lark_app_id or not settings.lark_app_secret:
        raise RuntimeError("LARK_APP_ID and LARK_APP_SECRET are required")

    await runner.validate()

    channel = FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret,
    )
    port = FeishuOutbound(channel)

    async def on_message(msg) -> None:
        inbound = _to_inbound(msg)
        logger.info(
            "message chat_type=%s sender=%s chat_id=%s text=%r",
            inbound.chat_type,
            inbound.operator_id,
            inbound.chat_id,
            redact_text(inbound.text[:120]),
        )

        if not inbound.chat_id:
            return

        # Group: only respond when @bot; DM: always respond.
        if inbound.chat_type != "p2p" and not inbound.mentioned_bot:
            return

        # Bootstrap hint: when no allowlist is set, surface the sender's
        # open_id once so the operator can paste it into .env.
        if not settings.lark_allowed_open_id:
            hint = (
                f"Bootstrap：你的 open_id 是 `{inbound.operator_id}`\n"
                f"请写入 .env：LARK_ALLOWED_OPEN_ID={inbound.operator_id}\n"
                f"然后重启 feishu_bot.py。"
            )
            await port.send_new(inbound, hint)
            logger.warning("LARK_ALLOWED_OPEN_ID unset; logged sender open_id=%s", inbound.operator_id)
            return

        if not is_allowed(inbound, settings):
            logger.warning("Rejected unauthorized Feishu open_id=%s", inbound.operator_id)
            await port.send_new(inbound, "Unauthorized.")
            return

        try:
            await dispatch(inbound, port, settings, runner)
        except Exception:
            logger.exception("Failed to handle Feishu message")

    channel.on("message", on_message)

    logger.info(
        "Feishu bot connecting (app_id=%s workspace=%s allowed_open_id=%s)",
        settings.lark_app_id,
        settings.codex_workspace_root,
        settings.lark_allowed_open_id or "(bootstrap mode)",
    )
    await channel.connect()


if __name__ == "__main__":
    asyncio.run(main())
