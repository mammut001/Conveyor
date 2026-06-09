"""channel/types.py — channel-agnostic inbound/outbound models.

Handlers depend only on these types and the OutboundPort protocol.
Each IM SDK (python-telegram-bot, lark-oapi) lives in its own
channel/* module and is responsible for converting SDK objects
to/from these models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

ChannelName = Literal["telegram", "feishu"]
ChatType = Literal["p2p", "group", "unknown"]


@dataclass(frozen=True)
class InboundMessage:
    """A single message arriving on any channel. Immutable."""
    channel: ChannelName
    operator_id: str
    chat_id: str
    message_id: str | None
    text: str
    chat_type: ChatType = "unknown"
    mentioned_bot: bool = False
    # Raw SDK payload, used by adapter-specific UI (e.g. inline buttons).
    # Handlers must not branch on this; it is purely for adapter handoff.
    raw: Any = None


class OutboundPort(Protocol):
    """Minimum surface handlers use to talk back to the operator.

    Telegram implements this via edit_message_text / send_message.
    Feishu implements it via FeishuChannel.send (throttled or card-stream).
    Optional capabilities are advertised via `supports_*` flags; handlers
    must check before calling the corresponding method.
    """
    supports_inline_buttons: bool

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        """Reply to a message; returns the new placeholder id (if any)."""
        ...

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        """Send a fresh message (not a reply); returns its id."""
        ...

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        """Edit an existing placeholder; returns False if the adapter
        has latched and downstream calls should fall back to send_new."""
        ...

    async def reply_with_buttons(
        self, msg: InboundMessage, text: str, buttons: list[list[dict]]
    ) -> str | None:
        """Optional. Reply with an inline button grid.
        Each button dict: {"text": ..., "callback_data": ...}."""
        ...
