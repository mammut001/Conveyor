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
connect, message routing, the bootstrap / allowlist gates, and the
Feishu card action callback handler.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from lark_oapi.channel import FeishuChannel

from channel.auth import is_allowed
from channel.feishu import FeishuOutbound, inbound_from_event
from channel.feishu_cards import action_to_command, extract_card_action
from channel.types import InboundMessage
from config import load_feishu_settings
from handlers import dispatch
from handlers.tools.runner import cancel_pending, execute_confirmed
from redaction import redact_text
from runner import CodexRunner

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("codex_feishu_bot")

settings = load_feishu_settings()
runner = CodexRunner(settings)


def _extract_card_action_event(msg: Any) -> tuple[InboundMessage, dict] | None:
    """Thin wrapper around :func:`channel.feishu_cards.extract_card_action`
    that converts the identity dict into an ``InboundMessage`` so the
    rest of the bot can treat the callback like any other message.
    """
    extracted = extract_card_action(msg)
    if extracted is None:
        return None
    identity, payload = extracted
    inbound = InboundMessage(
        channel="feishu",
        operator_id=identity["operator_id"],
        chat_id=identity["chat_id"],
        message_id=identity["message_id"] or None,
        text="",
        chat_type="p2p",  # card callbacks are p2p from the bot's view
        mentioned_bot=False,
        raw=msg,
    )
    return inbound, payload


async def _handle_card_action(msg: Any) -> None:
    """Card button click: validate sender, map action, dispatch.

    Confirmation actions reuse the existing token-based binding
    (operator + chat + channel + TTL). Slash-style actions
    (status / diff / apply / discard / cancel) synthesize a slash
    command and re-enter the regular dispatch path so behavior
    matches a typed command. Unknown / malformed actions get a
    short safe error.
    """
    extracted = _extract_card_action_event(msg)
    if extracted is None:
        logger.warning("Card action: missing or malformed event payload")
        return
    inbound, payload = extracted
    if not inbound.chat_id:
        return

    # Bootstrap / allowlist gates: card buttons must obey the same
    # allowlist as typed messages. Bootstrap mode echoes the
    # sender's open_id once; subsequent card presses before
    # allowlist is set are ignored.
    if not settings.lark_allowed_open_id:
        logger.warning(
            "Card action before allowlist: open_id=%s action=%s",
            inbound.operator_id, payload["action"],
        )
        return
    if not is_allowed(inbound, settings):
        logger.warning(
            "Rejected card action from unauthorized open_id=%s action=%s",
            inbound.operator_id, payload["action"],
        )
        return

    # We need a port for the reply. Reuse the shared port so
    # card-action replies go through the same code path as typed
    # messages (with card / text fallback).
    port = FeishuOutbound(_get_channel())

    action = payload["action"]
    try:
        if action in ("confirm", "cancel_confirm"):
            token = payload.get("token", "")
            if not token:
                await port.send_new(inbound, "无效的确认 token。")
                return
            if action == "confirm":
                await execute_confirmed(inbound, port, settings, token)
            else:
                await cancel_pending(inbound, port, settings, token)
            return
        cmd = action_to_command(action)
        if cmd is None:
            await port.send_new(inbound, f"未知卡片操作: {action}")
            return
        # Synthesize a typed slash command and re-enter the regular
        # dispatch path. parse_command will recognize it.
        inbound.text = f"/{cmd}"
        await dispatch(inbound, port, settings, runner)
    except Exception:
        logger.exception("Card action handler failed: action=%s", action)


# ---- Shared channel holder -------------------------------------------------

# FeishuChannel is created in main(); the card callback handler is
# registered before the channel is connected, so we expose the
# channel through a module-level holder for late binding.
_channel_holder: dict[str, Any] = {}


def _get_channel() -> Any:
    return _channel_holder.get("channel")


async def main() -> None:
    if not settings.lark_app_id or not settings.lark_app_secret:
        raise RuntimeError("LARK_APP_ID and LARK_APP_SECRET are required")

    await runner.validate()

    channel = FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret,
    )
    _channel_holder["channel"] = channel
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
    # Card action callback (Feishu / Lark Open Platform event_type
    # `card.action.trigger`). The actual subscription also needs the
    # matching event enabled in the Feishu developer console — see
    # README "Feishu setup" → "Card callbacks".
    channel.on("card.action.trigger", _handle_card_action)

    logger.info(
        "Feishu bot connecting (app_id=%s workspace=%s allowed_open_id=%s)",
        settings.lark_app_id,
        settings.codex_workspace_root,
        settings.lark_allowed_open_id or "(bootstrap mode)",
    )
    await channel.connect()


if __name__ == "__main__":
    asyncio.run(main())
