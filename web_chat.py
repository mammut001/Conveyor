"""web_chat.py — Web console chat bridge with SSE streaming and tool approvals."""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import time
import uuid
from typing import Any

from channel.types import InboundMessage, OutboundPort
from handlers.tools.confirm import PendingToolAction, get_pending, _CONFIRM_TTL_SECONDS
from redaction import redact_text, truncate
from transcript_store import get_transcript_store, session_identity

logger = logging.getLogger("conveyor.web_chat")


WEB_CHAT_PREFIX = "webchat-"
APPROVAL_PROMPT_PREFIX = "⚠️ 危险操作需确认"


def _reserved_web_agent_unavailable(control: Any, source_chat_id: str) -> bool:
    """True when a reserved web agent chat must not be opened.

    Primary ids (``agent-<id>``) and secondary ids (``agent-<id>-s-<hex>``)
    both require the agents feature, an active owned agent, and an unarchived
    registry view. Unknown, missing, archived, or disabled targets fail closed.
    Any other chat id is left to the generic web session path.
    """
    import agents
    from worker_sessions import WorkerSessionStore

    chat_id = str(source_chat_id or "")
    if not chat_id.startswith(agents.AGENT_CHAT_PREFIX):
        return False
    settings = getattr(control, "settings", None)
    if settings is None or not agents.enabled(settings):
        return True
    try:
        sessions = WorkerSessionStore(settings)
        store = agents.AgentStore(settings)
        if sessions.is_secondary_chat_id(chat_id):
            owner = sessions.owner_agent_id(agents.WEB_CHANNEL, chat_id)
            if not owner:
                return True
            agent = store.get(owner)
            return not agent or bool(agent.get("archived"))
        agent_id = chat_id[len(agents.AGENT_CHAT_PREFIX):]
        agent = store.get(agent_id)
        if not agent or agent.get("archived"):
            return True
        row = sessions.get(agents.session_id_for(agent_id))
        return (
            row is None
            or bool(row.get("archived"))
            or row.get("source_chat_id") != chat_id
            or row.get("channel") != agents.WEB_CHANNEL
        )
    except (OSError, sqlite3.Error):
        return True


def resolve_or_create_session(
    control: Any,
    requested_session_id: str,
    *,
    new_prefix: str = "web-",
) -> tuple[str, str, str, str] | None:
    """Return (channel, operator_id, source_chat_id, durable_session_id) or None if invalid."""
    if requested_session_id:
        resolved = control.resolve_session_identity(requested_session_id)
        if resolved:
            channel, operator_id, source_chat_id = resolved
            if channel == "web" and _reserved_web_agent_unavailable(control, source_chat_id):
                return None
            return channel, operator_id, source_chat_id, requested_session_id
        # A durable web id handed out by a previous /api/chat call whose turn
        # has not been persisted yet: keep using it instead of rejecting it.
        durable_prefix = "web:web-console:"
        if requested_session_id.startswith(durable_prefix):
            requested_session_id = requested_session_id[len(durable_prefix):]
    source_chat_id = requested_session_id or f"{new_prefix}{uuid.uuid4().hex[:12]}"
    if len(source_chat_id) > 128 or not all(ch.isalnum() or ch in "-_" for ch in source_chat_id):
        return None
    # A well-formed secondary id is not a license to invent a session.
    if _reserved_web_agent_unavailable(control, source_chat_id):
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
        self._approval_emitted = False

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.queue.put((event, data))

    def emit_subagent(self, data: dict[str, Any]) -> None:
        self.emit("subagent", data)

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        if text.startswith("💭") or text.startswith("⏳"):
            return self.placeholder_id
        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return self.placeholder_id

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        if self._is_redundant_note(text):
            return "web-chat-msg"
        if text.startswith("💭") or text.startswith("⏳"):
            return self.placeholder_id
        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return "web-chat-msg"

    def _is_redundant_note(self, text: str) -> bool:
        # ask_chat's default placeholder note after a confirmation request;
        # the approval card already says it.
        return self._approval_emitted and text.strip() == "已请求确认"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        if self._is_redundant_note(text):
            return True
        if text.endswith(" ▍"):
            clean = text[:-2].strip()
            self.emit("delta", {"text": clean})
            return True
        if text.startswith("🔎 搜索") or text.startswith("↪️") or text.startswith("🧩"):
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
                expires_in = max(0, int(pending.expires_at - time.time()))

            self._approval_emitted = True
            self.emit("approval", {
                "id": token,
                "tool_name": tool_name,
                "arg": arg,
                "summary": summary,
                "text": text,
                "expires_in_seconds": expires_in,
            })
            self._persist_turn(
                msg, text, kind="tool_approval",
                metadata={"approval_id": token, "tool_name": tool_name, "arg": arg},
            )
            return "web-chat-approval"

        self.emit("message", {"text": text})
        self._persist_turn(msg, text, kind="chat")
        return "web-chat-msg"

    async def fetch_attachment(self, msg: InboundMessage, attachment: Any) -> bytes | None:
        return None

    def _persist_turn(
        self,
        msg: InboundMessage,
        text: str,
        *,
        kind: str = "chat",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._delivered_final or not self.settings or not self.durable_session_id:
            return
        self._delivered_final = True
        user_text = self.prompt or msg.text
        ident = dict(channel=msg.channel, operator_id=msg.operator_id, source_chat_id=msg.chat_id)
        try:
            store = get_transcript_store(self.settings)
            store.append(self.durable_session_id, "user", user_text, kind="chat", **ident)
            store.append(
                self.durable_session_id, "assistant", text, kind=kind, metadata=metadata, **ident,
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
    from handlers.chat import ask_chat, readonly_host_tool
    from handlers.intent import RouteResult, computer_chat_route
    from handlers.tools.runner import handle_route
    try:
        host_tool = readonly_host_tool(prompt, settings)
        if host_tool:
            await handle_route(
                msg, port, runner, settings,
                RouteResult(kind="deterministic", tools=(host_tool,)),
            )
            port.emit("done", {"outcome": "answered"})
            return
        # Same natural-language desktop route the phone bots already run.
        # A chat answer must not swallow "打开计算器".
        desktop = computer_chat_route(prompt, settings)
        if desktop is not None:
            await handle_route(msg, port, runner, settings, desktop)
            port.emit("done", {"outcome": "answered"})
            return
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
            kind="tool_result",
            metadata={
                "approval_id": token,
                "tool_name": pending.tool_name,
                "decision": "approved" if approve else "denied",
            },
        )
    except Exception:
        logger.exception("Failed to append tool approval result to transcript store")

    try:
        from routines import record_approval_decision
        decision_label = "approved" if approve else "denied"
        record_approval_decision(settings, token, decision_label, safe_result)
    except Exception:
        logger.debug("Failed to record routine approval decision", exc_info=True)

    return {
        "id": token,
        "kind": "tool",
        "status": status,
        "result": safe_result,
    }


def build_history(session: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Transcript messages for the chat view, with approval prompts resolved.

    An approval prompt is shown as pending only while its token is still live;
    otherwise it is resolved from the later decision record (approved/denied)
    or reported as expired, so reloading never shows a stale live prompt.
    """
    raw = list((session or {}).get("messages") or [])
    decisions: dict[str, str] = {}
    for m in raw:
        meta = m.get("metadata") or {}
        if m.get("kind") == "tool_result" and meta.get("approval_id"):
            decisions[str(meta["approval_id"])] = str(meta.get("decision") or "")
    out: list[dict[str, Any]] = []
    for m in raw:
        meta = m.get("metadata") or {}
        item: dict[str, Any] = {
            "role": m.get("role"),
            "text": m.get("content") or "",
            "created_at": m.get("created_at") or "",
            "kind": m.get("kind") or "",
        }
        text = item["text"]
        if m.get("kind") == "tool_approval" and meta.get("approval_id"):
            token = str(meta["approval_id"])
            pending = get_pending(token)
            status = decisions.get(token) or ("pending" if pending else "expired")
            item["approval"] = {
                "id": token,
                "tool_name": meta.get("tool_name") or "",
                "arg": meta.get("arg") or "",
                "status": status,
            }
            if pending is not None and status == "pending":
                item["approval"]["expires_in_seconds"] = max(
                    0, int(pending.expires_at - time.time())
                )
        elif m.get("role") == "assistant" and text.startswith(APPROVAL_PROMPT_PREFIX):
            # Legacy prompt without metadata: no longer decidable, outcome unknown.
            item["approval"] = {"id": "", "tool_name": "", "arg": "", "status": "closed"}
        out.append(item)
    return out
