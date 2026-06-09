"""channel/auth.py — single-operator allowlist check across channels.

Telegram: operator_id is the stringified user id.
Feishu:   operator_id is the open_id ("ou_xxx").
"""
from __future__ import annotations

from config import Settings
from channel.types import ChannelName, InboundMessage


def is_allowed(msg: InboundMessage, settings: Settings) -> bool:
    if msg.channel == "telegram":
        return msg.operator_id == str(settings.telegram_allowed_user_id)
    if msg.channel == "feishu":
        # bootstrap mode: when LARK_ALLOWED_OPEN_ID is unset, the
        # FeishuAdapter is responsible for the "echo your open_id"
        # hint, so let all messages through. Once the operator sets
        # LARK_ALLOWED_OPEN_ID we enforce the match.
        if not settings.lark_allowed_open_id:
            return True
        return msg.operator_id == settings.lark_allowed_open_id
    return False
