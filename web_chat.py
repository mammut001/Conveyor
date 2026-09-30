"""web_chat.py — Web console chat bridge with SSE streaming and tool approvals."""
from __future__ import annotations

import json
import logging
import queue
import time
import uuid
from typing import Any

from channel.types import InboundMessage, OutboundPort
from handlers.tools.confirm import PendingToolAction, get_pending, _CONFIRM_TTL_SECONDS
from redaction import redact_text, truncate
from transcript_store import get_transcript_store, session_identity

logger = logging.getLogger("conveyor.web_chat")


def resolve_or_create_session(
    control: Any,
    requested_session_id: str,
) -> tuple[str, str, str, str] | None:
    """Return (channel, operator_id, source_chat_id, durable_session_id) or None if invalid."""
    if requested_session_id:
        resolved = control.resolve_session_identity(requested_session_id)
        if resolved:
            channel, operator_id, source_chat_id = resolved
            return channel, operator_id, source_chat_id, requested_session_id
    source_chat_id = requested_session_id or f"web-{uuid.uuid4().hex[:12]}"
    if len(source_chat_id) > 128 or not all(ch.isalnum() or ch in "-_" for ch in source_chat_id):
        return None
    channel, operator_id = "web", "web-console"
    durable_session_id = session_identity(channel, source_chat_id, operator_id)
    return channel, operator_id, source_chat_id, durable_session_id


class WebChatPort(OutboundPort):
    supports_inline_buttons = True
    supports_attachments = False
    handles_transcript_directly = True

    def __init__(
        self,
        event_queue: queue.Queue,
        settings: Any,
        durable_session_id: str,
        *,
        prompt: str = "",
    ) -> None:
        self.queue = event_queue
        self.settings = settings
        self.durable_session_id = durable_session_id
        self.prompt = prompt
        self._delivered_final = False
        self.placeholder_id = "web-chat-placeholder"

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.queue.put((event, data))

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        if text.startswith("💭") or text.startswith("⏳"):
            return self.placeholder_id
        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return self.placeholder_id

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        if text.startswith("💭") or text.startswith("⏳"):
            return self.placeholder_id
        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return "web-chat-msg"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        if text.endswith(" ▍"):
            clean = text[:-2].strip()
            self.emit("delta", {"text": clean})
            return True
        if text.startswith("🔎 搜索") or text.startswith("↪️"):
            self.emit("status", {"text": text})
            return True
        # Final answer delivered via edit_progress
        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return True

    async def reply_with_buttons(
        self, msg: InboundMessage, text: str, buttons: list[list[dict]]
    ) -> str | None:
        token = None
        for row in buttons:
            for btn in row:
                cb = btn.get("callback_data", "")
                if cb.startswith("tool:confirm:"):
                    token = cb[len("tool:confirm:"):]
                    break
            if token:
                break

        if token:
            pending = get_pending(token)
            tool_name = pending.tool_name if pending else ""
            arg = pending.arg if pending else ""
            summary = ""
            for line in text.splitlines():
                if line.startswith("说明:"):
                    summary = line[len("说明:"):].strip()
                    break
            if not summary and pending:
                from handlers.tools.registry import get_tool
                spec = get_tool(pending.tool_name)
                summary = spec.summary if spec else pending.tool_name

            expires_in = int(_CONFIRM_TTL_SECONDS)
            if pending:
                expires_in = max(0, int(_CONFIRM_TTL_SECONDS - (time.time() - pending.created_at)))

            self.emit("approval", {
                "id": token,
                "tool_name": tool_name,
                "arg": arg,
                "summary": summary,
                "text": text,
                "expires_in_seconds": expires_in,
            })
            self._persist_turn(msg, text, kind="chat")
            return "web-chat-approval"

        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return "web-chat-msg"

    async def fetch_attachment(self, msg: InboundMessage, attachment: Any) -> bytes | None:
        return None

    def _persist_turn(self, msg: InboundMessage, text: str, *, kind: str = "chat") -> None:
        if self._delivered_final or not self.settings or not self.durable_session_id:
            return
        self._delivered_final = True
        user_text = self.prompt or msg.text
        try:
            get_transcript_store(self.settings).append_turn(
                self.durable_session_id,
                user_text,
                text,
                channel=msg.channel,
                operator_id=msg.operator_id,
                source_chat_id=msg.chat_id,
                kind=kind,
            )
        except Exception:
            logger.exception("Failed to persist web chat turn to TranscriptStore")


async def run_web_chat(
    msg: InboundMessage,
    port: WebChatPort,
    settings: Any,
    runner: Any,
    prompt: str,
) -> None:
    from handlers.chat import ask_chat
    try:
        outcome, checked = await ask_chat(
            msg, port, settings, question=prompt, runner=runner,
        )
        if outcome == "escalate":
            reason = f": {checked.reason}" if checked and checked.reason else ""
            msg_text = f"This request requires task execution{reason}. Please submit it as a task using the composer."
            port.emit("message", {"text": msg_text})
            port._persist_turn(msg, msg_text, kind="chat")
        elif outcome == "unavailable":
            msg_text = "The chat tier is currently unavailable. Please submit your request as a task using the composer."
            port.emit("message", {"text": msg_text})
            port._persist_turn(msg, msg_text, kind="chat")
        port.emit("done", {"outcome": outcome})
    except Exception:
        logger.exception("Error in run_web_chat")
        port.emit("error", {"error": "Internal error"})
        port.emit("done", {"outcome": "error"})


class CollectingPort:
    supports_inline_buttons = False
    supports_attachments = False

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        self.replies.append(text)
        return "ok"

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        self.replies.append(text)
        return "ok"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.replies.append(text)
        return True

    @property
    def result_text(self) -> str:
        return "\n".join(self.replies).strip()


async def decide_tool_approval(
    pending: PendingToolAction,
    approve: bool,
    settings: Any,
) -> dict[str, Any]:
    from handlers.tools.runner import cancel_pending, execute_confirmed

    port = CollectingPort()
    msg = InboundMessage(
        channel=pending.channel,
        operator_id=pending.operator_id,
        chat_id=pending.chat_id,
        message_id=uuid.uuid4().hex,
        text="确认" if approve else "取消",
        chat_type="p2p",
    )

    token = pending.token
    # Re-check on the event loop: execute_confirmed pops the token without an
    # intervening await, so a concurrent second decision sees it as gone.
    if get_pending(token) is None:
        return {"id": token, "kind": "tool", "status": "expired", "result": ""}
    if approve:
        await execute_confirmed(msg, port, settings, token)
        status = "accepted"
    else:
        await cancel_pending(msg, port, settings, token)
        status = "rejected"

    raw_result = port.result_text
    safe_result = truncate(redact_text(raw_result), 4_000)

    # Append the result to the session transcript as an assistant message
    try:
        durable_session_id = session_identity(pending.channel, pending.chat_id, pending.operator_id)
        transcript_store = get_transcript_store(settings)
        transcript_store.append(
            durable_session_id,
            "assistant",
            safe_result,
            channel=pending.channel,
            operator_id=pending.operator_id,
            source_chat_id=pending.chat_id,
            kind="tool",
        )
    except Exception:
        logger.exception("Failed to append tool approval result to transcript store")

    return {
        "id": token,
        "kind": "tool",
        "status": status,
        "result": safe_result,
    }
