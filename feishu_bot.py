#!/usr/bin/env python3
"""Feishu/Lark bot — WebSocket long connection + handlers/dispatch.

Run locally while configuring the Feishu developer console (Events must see
an online long-connection client before saving im.message.receive_v1).

  export LARK_APP_ID=cli_xxx
  export LARK_APP_SECRET=xxx
  # optional until first message: LARK_ALLOWED_OPEN_ID=ou_xxx
  .venv/bin/python feishu_bot.py

P2.1: the Feishu adapter (OutboundPort + inbound conversion) lives in
`channel/feishu.py`. This entrypoint only handles startup, WebSocket
connect, message routing, and the bootstrap / allowlist gates.
"""
from __future__ import annotations

import asyncio
import logging

from lark_oapi.channel import FeishuChannel

from channel.auth import is_allowed
from channel.feishu import FeishuOutbound, inbound_from_event
from config import load_feishu_settings
from handlers import dispatch
from redaction import redact_text
from runner import CodexRunner

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codex_feishu_bot")

settings = load_feishu_settings()
runner = CodexRunner(settings)


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
        inbound = inbound_from_event(msg)
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
