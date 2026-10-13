"""Optional /memory v2 subcommands. Existing /memory (daily journal) remains intact."""
from __future__ import annotations

from typing import Any

from personal_tools.long_term_memory import GROUP_REFUSAL, allowed_for
from personal_tools import memory_v2_bridge


async def handle(msg: Any, port: Any, settings: Any, arg: str) -> bool:
    if not getattr(settings, "memory_v2_enabled", False):
        return False
    action, _, rest = (arg or "").strip().partition(" ")
    if action.lower() not in ("show", "search", "forget"):
        return False
    if not allowed_for(settings, msg):
        await port.reply(msg, GROUP_REFUSAL)
        return True
    if action.lower() == "show":
        result = await memory_v2_bridge.memory_list(
            settings, operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id,
        )
        await port.reply(msg, result.text)
    elif action.lower() == "search":
        result = await memory_v2_bridge.memory_search(
            settings, rest, operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id,
        )
        await port.reply(msg, result.text)
    else:
        screened = memory_v2_bridge.screen_write_arg("memory.forget", rest)
        if screened.error:
            await port.reply(msg, screened.error)
            return True
        # The slash command is NOT a shortcut around WRITE confirmation.
        from handlers.tools.runner import _request_confirmation
        await _request_confirmation(msg, port, settings, "memory.forget", screened.arg)
    return True
