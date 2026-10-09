"""channel/types.py — channel-agnostic inbound/outbound models.

Handlers depend only on these types and the OutboundPort protocol.
Each IM SDK (python-telegram-bot, lark-oapi) lives in its own
channel/* module and is responsible for converting SDK objects
to/from these models.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

ChannelName = Literal["telegram", "feishu", "web"]
ChatType = Literal["p2p", "group", "unknown"]


@dataclass(frozen=True)
class ReplyContext:
    """The message an inbound message replies to or quotes.

    This is what makes "@bot is this true?" under someone else's message
    work: the adapter captures the replied-to text so handlers can answer
    about it. The text is third-party content and must be treated as
    untrusted data by anything that forwards it to an agent.
    """
    text: str
    author: str = ""
    from_bot: bool = False
    # True when the user selected only part of the replied-to message
    # (Telegram quote) rather than replying to the whole message.
    partial_quote: bool = False


@dataclass(frozen=True)
class Attachment:
    """An image attached to (or replied to by) an inbound message.

    Only a reference is captured at conversion time; the bytes are fetched
    through ``OutboundPort.fetch_attachment`` after the allowlist check.
    """
    kind: Literal["image"]
    ref: str  # Telegram file_id / Feishu image_key
    origin: Literal["message", "reply"] = "message"
    message_id: str | None = None  # Feishu: message owning the resource
    size: int | None = None  # bytes, when the channel reports it


@dataclass(frozen=True)
class InboundMessage:
    """A single message arriving on any channel. Immutable."""
    channel: ChannelName
    operator_id: str
    # Logical conversation address. Telegram may include topic and agent;
    # channel transports decode it before calling the Bot API.
    chat_id: str
    message_id: str | None
    text: str
    chat_type: ChatType = "unknown"
    mentioned_bot: bool = False
    reply_to: ReplyContext | None = None
    attachments: tuple[Attachment, ...] = ()
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
    supports_attachments: bool = False

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

    async def fetch_attachment(
        self, msg: InboundMessage, attachment: Attachment,
    ) -> bytes | None:
        """Optional (``supports_attachments``). Download an attachment's
        bytes; None when unavailable or too large."""
        ...

    async def send_image(
        self,
        chat_id: str,
        image_path: str,
        *,
        caption: str | None = None,
    ) -> None:
        """Send an image file to the specified chat/user."""
        ...
