"""Explicit origin-channel G3 confirmation for Telegram.

Normal relay publish notifies OTHER channels; this module sends the
operator's original Telegram chat its own actionable form-preview card.
"""
from __future__ import annotations
import asyncio

async def origin_telegram(settings, pending, summary):
    from approval_relay import (
        ApprovalNotificationRequest,
        TelegramRelayNotifier,
        _record_notification,
    )
    target=str(pending.chat_id)
    req=ApprovalNotificationRequest(
        token=pending.token,channel="telegram",target=target,
        tool_name=pending.tool_name,summary=summary,danger="write",
        arg_preview=pending.arg,source="chat",expires_at=pending.expires_at)
    try:
        external_id=await asyncio.to_thread(TelegramRelayNotifier(settings).send,req)
    except Exception:
        return False
    if not external_id:
        return False
    _record_notification(settings,pending.token,"telegram",target,external_id)
    return True
