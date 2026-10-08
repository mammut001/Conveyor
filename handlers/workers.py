"""Workers list, session switch, and physical-origin delivery.

``/workers`` lists the same canonical sessions as the Web console and can bind
the next message on this physical chat to one of them. Replies still go to the
chat the operator typed in. Callback tokens (``wk:``) carry the captured
session; callers only present the token plus the authenticated scope.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agents import AgentError, AgentStore, agent_for_chat, enabled
from channel.types import InboundMessage
from redaction import redact_text
from transcript_store import session_identity
from worker_sessions import WorkerSessionStore

_PAGE = 5
_NAV = frozenset({"list", "open", "back", "continue", "tasks", "switch", "new", "select", "exit"})
_JOB = frozenset({"status", "jobs", "cancel", "diff", "apply", "discard"})
_GATED = frozenset({"confirm", "cancel_confirm", "deep"})


@dataclass(frozen=True)
class PhysicalOrigin:
    channel: str
    operator_id: str
    chat_id: str
    chat_type: str = "unknown"
    message_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "operator_id": self.operator_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "message_id": self.message_id,
        }


class PhysicalOriginPort:
    """Deliver through the originating IM chat, whatever execution identity is set.

    Canonical web ids are never passed to the IM API. Tool and deep buttons
    minted during canonical execution become ``wk:`` tokens that still remember
    that canonical session.
    """

    supports_attachments = False

    def __init__(self, inner: Any, origin: PhysicalOrigin, settings: Any) -> None:
        self._inner = inner
        self.origin = origin
        self.delivery_origin = origin.as_dict()
        self.settings = settings
        self.supports_inline_buttons = True
        self.supports_attachments = bool(getattr(inner, "supports_attachments", False))
        # Telegram sets this False so the bot process can accept /cancel while a job runs.
        # A missing flag stays True, matching the historical chat-adapter default.
        inner_wait = getattr(inner, "wait_for_job", True)
        self.wait_for_job = inner_wait if isinstance(inner_wait, bool) else True

    def _physical(self, msg: InboundMessage) -> InboundMessage:
        return replace(
            msg,
            channel=self.origin.channel,  # type: ignore[arg-type]
            operator_id=self.origin.operator_id,
            chat_id=self.origin.chat_id,
            chat_type=self.origin.chat_type or msg.chat_type,  # type: ignore[arg-type]
            message_id=self.origin.message_id or msg.message_id,
        )

    def _translate(self, msg: InboundMessage, buttons: list[list[dict]]) -> list[list[dict]] | None:
        """Return translated buttons, or None when a tool/deep button cannot be scoped.

        Never fall back to the original callback. That would publish a canonical
        tool token on the IM client.
        """
        store = WorkerSessionStore(self.settings)
        session_id = session_identity(msg.channel, msg.chat_id, msg.operator_id)
        rows: list[list[dict]] = []
        for row in buttons:
            translated = []
            for button in row:
                data = str(button.get("callback_data") or "")
                action, _extra = _captured_action(data)
                if action is None:
                    translated.append(dict(button))
                    continue
                try:
                    token = store.issue_token(
                        operator_id=self.origin.operator_id,
                        channel=self.origin.channel,
                        topic=self.origin.chat_id,
                        agent_id=_agent_of(self.settings, session_id),
                        session_id=session_id,
                        action=action,
                        extra={"native": data},
                    )
                except AgentError:
                    return None
                translated.append({"text": button.get("text") or action, "callback_data": f"wk:{token}"})
            if translated:
                rows.append(translated)
        return rows

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        return await self._inner.reply(self._physical(msg), text)

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        return await self._inner.send_new(self._physical(msg), text)

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        return await self._inner.edit_progress(self._physical(msg), placeholder_id, text)

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list[list[dict]]) -> str | None:
        physical = self._physical(msg)
        translated = self._translate(msg, buttons)
        if translated is None:
            return await self._inner.reply(physical, f"{text}\n\n确认按钮没能安全生成，请重新发送以刷新。")
        if self.origin.channel == "feishu" and hasattr(self._inner, "send_card"):
            return await self._inner.send_card(physical, workers_card(text, _pairs(translated)))
        if getattr(self._inner, "supports_inline_buttons", False):
            return await self._inner.reply_with_buttons(physical, text, translated)
        return await self._inner.reply(physical, text)

    async def fetch_attachment(self, msg: InboundMessage, attachment: Any) -> bytes | None:
        return await self._inner.fetch_attachment(self._physical(msg), attachment)

    async def send_image(self, chat_id: str, image_path: str, *, caption: str | None = None) -> None:
        await self._inner.send_image(self.origin.chat_id, image_path, caption=caption)

    async def send_card(self, msg: InboundMessage, card: dict, *, reply_to: str | None = None) -> str | None:
        return await self._inner.send_card(self._physical(msg), card, reply_to=reply_to)


def workers_card(text: str, buttons: list[dict[str, str]]) -> dict[str, Any]:
    """Feishu card whose buttons carry only ``action=workers`` and a token."""
    actions = []
    for button in buttons:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": button["text"][:30]},
            "type": "default",
            "value": {"action": "workers", "token": button["token"]},
        })
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": text[:3000]}]
    for start in range(0, len(actions), 2):
        elements.append({"tag": "action", "actions": actions[start:start + 2]})
    return {
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "Workers"}, "template": "blue"},
        "elements": elements,
    }


def _pairs(rows: list[list[dict]]) -> list[dict[str, str]]:
    found = []
    for row in rows:
        for button in row:
            data = str(button.get("callback_data") or "")
            if data.startswith("wk:"):
                found.append({"text": str(button.get("text") or ""), "token": data[3:]})
    return found


def _captured_action(data: str) -> tuple[str | None, dict[str, str]]:
    if data.startswith("tool:confirm:"):
        return "confirm", {}
    if data.startswith("tool:cancel:"):
        return "cancel_confirm", {}
    if data == "deep" or data.startswith("deep:"):
        return "deep", {}
    return None, {}


def _agent_of(settings: Any, session_id: str) -> str:
    session = WorkerSessionStore(settings).get(session_id)
    if session is None:
        raise AgentError("session is not available")
    return str(session["agent_id"])


def _source(store: WorkerSessionStore, msg: InboundMessage) -> str:
    return store._physical_source(msg.channel, msg.chat_id)


def legacy_job_card_allowed(settings: Any, msg: InboundMessage, job_id: str) -> bool:
    """Old Feishu job cards may run only when no Workers session is selected,
    or when their job id belongs to the selected canonical session.
    """
    if not enabled(settings) or msg.channel not in ("telegram", "feishu"):
        return True
    try:
        selected = WorkerSessionStore(settings).selected(msg.channel, msg.chat_id, msg.operator_id)
    except AgentError:
        return False
    if selected is None:
        return True
    job_id = str(job_id or "")
    if not job_id:
        return False
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    if not path.exists():
        return False
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        row = conn.execute(
            "SELECT channel, operator_id, chat_id FROM queued_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    if row is None:
        return False
    return (
        str(row[0]) == selected["channel"]
        and str(row[1]) == selected["operator_id"]
        and str(row[2]) == selected["source_chat_id"]
    )


def selection_conflict(settings: Any, msg: InboundMessage, channel: str, chat_id: str) -> str | None:
    """Reject a legacy callback that would run under a different Workers session.

    Returns an operator-facing error, or None when the click may proceed on
    its original conversation. A matching selection is allowed. No selection
    keeps the legacy route. This never rewrites the click onto the selection.
    """
    if not enabled(settings) or msg.channel not in ("telegram", "feishu"):
        return None
    try:
        selected = WorkerSessionStore(settings).selected(msg.channel, msg.chat_id, msg.operator_id)
    except AgentError as exc:
        return str(exc)
    if selected is None:
        return None
    if selected["channel"] == channel and selected["source_chat_id"] == chat_id:
        return None
    return "这个按钮属于之前的对话。请切回原会话或发送 /workers exit 后再试。"


def bind_execution(msg: InboundMessage, port: Any, settings: Any) -> tuple[InboundMessage, Any]:
    """Rewrite execution onto the selected canonical session. Delivery stays physical."""
    if msg.channel not in ("telegram", "feishu") or not enabled(settings):
        return msg, port
    store = WorkerSessionStore(settings)
    selected = store.selected(msg.channel, msg.chat_id, msg.operator_id)
    if selected is None:
        return msg, port
    agent = agent_for_chat(settings, selected["channel"], selected["source_chat_id"])
    if not agent:
        raise AgentError("已选会话的 Agent 不可用，任务不会转到默认项目。发送 /workers exit 或 /agent 恢复。")
    origin = PhysicalOrigin(
        msg.channel,
        msg.operator_id,
        _source(store, msg),
        msg.chat_type,
        msg.message_id or "",
    )
    rewritten = replace(
        msg,
        channel=selected["channel"],  # type: ignore[arg-type]
        operator_id=selected["operator_id"],
        chat_id=selected["source_chat_id"],
    )
    return rewritten, PhysicalOriginPort(port, origin, settings)


_MARK = {"waiting": "🟠", "working": "🔵", "idle": "🟢"}
_STATUS = {"waiting": "等待确认", "working": "执行中", "idle": "空闲"}


def _live_jobs(settings: Any, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Running and queued jobs for this session, including ones behind newer terminal rows."""
    import sqlite3
    store = WorkerSessionStore(settings)
    conn = sqlite3.connect(str(store.path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, state, prompt FROM queued_jobs
               WHERE channel = ? AND operator_id = ? AND chat_id = ?
                 AND state IN ('queued', 'running')
               ORDER BY created_at ASC""",
            (session["channel"], session["operator_id"], session["source_chat_id"]),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        {"id": row["id"], "state": row["state"], "prompt_preview": redact_text(row["prompt"] or "")[:80]}
        for row in rows
    ]


def _session_state(settings: Any, session: dict[str, Any]) -> str:
    from handlers.tools.confirm import get_pending_for_context, shared_pending_contexts
    key = (session["channel"], session["operator_id"], session["source_chat_id"])
    try:
        if key in shared_pending_contexts():
            return "等待确认"
    except Exception:
        pass
    if get_pending_for_context(session["operator_id"], session["source_chat_id"], session["channel"]):
        return "等待确认"
    if _live_jobs(settings, session):
        return "执行中"
    return "空闲"


def _has_legacy_history(settings: Any, channel: str, operator_id: str, chat_id: str) -> bool:
    import sqlite3
    from transcript_store import get_transcript_store
    try:
        saved = get_transcript_store(settings).get_session(session_identity(channel, chat_id, operator_id))
    except ValueError:
        saved = None
    if saved and saved.get("messages"):
        return True
    store = WorkerSessionStore(settings)
    conn = sqlite3.connect(str(store.path), timeout=10.0)
    try:
        row = conn.execute(
            "SELECT 1 FROM queued_jobs WHERE channel = ? AND operator_id = ? AND chat_id = ? LIMIT 1",
            (channel, operator_id, chat_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def _discover_legacy(settings: Any, msg: InboundMessage, agent_id: str) -> None:
    """Register this operator's existing chat/topic legacy only. Never another chat."""
    if msg.channel != "telegram":
        return
    store = WorkerSessionStore(settings)
    source = _source(store, msg)
    bound = AgentStore(settings).bound_agent_id("telegram", source)
    identities: list[str] = []
    if bound == agent_id and _has_legacy_history(settings, "telegram", msg.operator_id, source):
        identities.append(source)
    suffixed = f"{source}:agent:{agent_id}"
    if _has_legacy_history(settings, "telegram", msg.operator_id, suffixed):
        identities.append(suffixed)
    from transcript_store import get_transcript_store
    from channel.telegram_identity import TelegramAddress
    try:
        listed = get_transcript_store(settings).list_sessions(200)
    except Exception:
        listed = []
    for item in listed:
        if item.get("channel") != "telegram" or str(item.get("operator_id") or "") != str(msg.operator_id):
            continue
        chat = str(item.get("source_chat_id") or "")
        try:
            addr = TelegramAddress.parse(chat)
        except ValueError:
            continue
        if addr.source != source:
            continue
        if addr.agent_id == agent_id or (addr.agent_id is None and bound == agent_id):
            identities.append(addr.conversation)
    for identity in dict.fromkeys(identities):
        try:
            store.register_legacy(
                agent_id, channel="telegram", operator_id=msg.operator_id,
                source_chat_id=identity, requester_operator=msg.operator_id,
                current_source=source,
            )
        except AgentError:
            continue


def _sessions_for(settings: Any, msg: InboundMessage, agent_id: str) -> list[dict[str, Any]]:
    _discover_legacy(settings, msg, agent_id)
    store = WorkerSessionStore(settings)
    rows = list(store.list(agent_id))
    source = _source(store, msg)
    for candidate in (source, f"{source}:agent:{agent_id}"):
        try:
            session_id = session_identity(msg.channel, candidate, msg.operator_id)
        except ValueError:
            continue
        found = store.get(session_id)
        if (
            found and found["kind"] == "legacy" and not found["archived"]
            and found["agent_id"] == agent_id and found["operator_id"] == str(msg.operator_id)
            and store._physical_source(found["channel"], found["source_chat_id"]) == source
        ):
            rows.append(found)
    return rows


def _catalog(settings: Any) -> list[dict[str, Any]]:
    """Same aggregate rows the Web console uses for every agent."""
    try:
        from handlers.job_queue import get_job_queue
        from web_control import WebControl
        payload = WebControl(settings, None, get_job_queue()).list_agents()
        agents = payload.get("agents") or []
        if agents:
            return agents
    except Exception:
        pass
    return [
        {"id": agent["id"], "name": agent["name"], "status": "idle", "last_message": "", "session_id": agent["session_id"]}
        for agent in AgentStore(settings).list()
    ]


async def _emit(msg: InboundMessage, port: Any, text: str, buttons: list[dict[str, str]]) -> None:
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        await port.send_card(msg, workers_card(text, buttons))
        return
    if buttons and getattr(port, "supports_inline_buttons", False):
        rows = [[{"text": button["text"], "callback_data": f"wk:{button['token']}"}] for button in buttons]
        await port.reply_with_buttons(msg, text, rows)
        return
    await port.reply(msg, text)


def _issue(
    store: WorkerSessionStore,
    msg: InboundMessage,
    session: dict[str, Any],
    action: str,
    page: int = 0,
    extra: dict[str, Any] | None = None,
) -> str:
    return store.issue_token(
        operator_id=msg.operator_id,
        channel=msg.channel,
        topic=_source(store, msg),
        agent_id=session["agent_id"],
        session_id=session["session_id"],
        action=action,
        page=page,
        extra=extra,
    )


def _anchor(settings: Any, agent_id: str) -> dict[str, Any]:
    rows = WorkerSessionStore(settings).list(agent_id)
    return next(row for row in rows if row["kind"] == "main")


def _open_session(settings: Any, msg: InboundMessage, agent_id: str) -> dict[str, Any]:
    """Selected session when it belongs to this agent; otherwise the main session."""
    store = WorkerSessionStore(settings)
    try:
        selected = store.selected(msg.channel, msg.chat_id, msg.operator_id)
    except AgentError:
        selected = None
    if selected and selected["agent_id"] == agent_id and not selected["archived"]:
        return selected
    return _anchor(settings, agent_id)


async def _render_list(msg: InboundMessage, port: Any, settings: Any, page: int) -> None:
    """Every worker, not the sessions of whichever agent this chat last used."""
    store = WorkerSessionStore(settings)
    rows = _catalog(settings)
    if not rows:
        await port.reply(msg, "还没有 Worker。")
        return
    pages = max(1, (len(rows) + _PAGE - 1) // _PAGE)
    page = max(0, min(page, pages - 1))
    window = rows[page * _PAGE:(page + 1) * _PAGE]
    lines = [f"我的 Workers {page + 1}/{pages}"]
    buttons = []
    for agent in window:
        status = str(agent.get("status") or "idle")
        mark = _MARK.get(status, "🟢")
        label = " ".join(str(agent.get("name") or agent["id"]).split())[:20]
        state = _STATUS.get(status, "空闲")
        preview = " ".join(redact_text(str(agent.get("last_message") or "")).split())[:60]
        lines.append(f"{mark} {label} · {state}" + (f"\n{preview}" if preview else ""))
        opened = _open_session(settings, msg, str(agent["id"]))
        buttons.append({
            "text": f"{mark}{label} {state}"[:40],
            "token": _issue(store, msg, opened, "open", page, {"agent_page": page}),
        })
    anchor = _open_session(settings, msg, str(window[0]["id"]))
    if page:
        buttons.append({"text": "上一页", "token": _issue(store, msg, anchor, "list", page - 1)})
    if page + 1 < pages:
        buttons.append({"text": "下一页", "token": _issue(store, msg, anchor, "list", page + 1)})
    buttons.append({"text": "退出 Workers", "token": _issue(store, msg, anchor, "exit", page)})
    await _emit(msg, port, "\n".join(lines), buttons)


def _detail_text(settings: Any, session: dict[str, Any]) -> str:
    from transcript_store import get_transcript_store
    agent = AgentStore(settings).get(session["agent_id"])
    jobs = _live_jobs(settings, session)
    state = _session_state(settings, session)
    message = get_transcript_store(settings).last_message(session["session_id"])
    preview = " ".join(redact_text(str((message or {}).get("content") or "")).split())[:160]
    task = "无"
    if jobs:
        recent = jobs[0]
        task = f"{recent['id']} · {recent['state']} · {recent['prompt_preview']}"
    return "\n".join([
        str((agent or {}).get("name") or session["agent_id"]),
        f"会话：{session['title']}",
        f"状态：{state}",
        f"最近消息：{preview or '无'}",
        f"任务：{task}",
    ])


async def _render_detail(msg: InboundMessage, port: Any, settings: Any, session: dict[str, Any], page: int) -> None:
    store = WorkerSessionStore(settings)
    extra = {"agent_page": page}
    buttons = [
        {"text": "💬继续对话", "token": _issue(store, msg, session, "continue", page, extra)},
        {"text": "📋查看任务", "token": _issue(store, msg, session, "tasks", page, extra)},
        {"text": "🔄切换会话", "token": _issue(store, msg, session, "switch", page, {**extra, "switch_page": 0})},
        {"text": "↩️返回列表", "token": _issue(store, msg, session, "back", page, extra)},
    ]
    await _emit(msg, port, _detail_text(settings, session), buttons)


async def _render_switch(
    msg: InboundMessage, port: Any, settings: Any, session: dict[str, Any], agent_page: int, switch_page: int,
) -> None:
    store = WorkerSessionStore(settings)
    rows = _sessions_for(settings, msg, session["agent_id"])
    pages = max(1, (len(rows) + _PAGE - 1) // _PAGE)
    switch_page = max(0, min(switch_page, pages - 1))
    window = rows[switch_page * _PAGE:(switch_page + 1) * _PAGE]
    buttons = []
    extra = {"agent_page": agent_page, "switch_page": switch_page}
    for choice in window:
        label = ("主会话 " if choice["kind"] == "main" else "") + str(choice["title"])[:24]
        buttons.append({
            "text": label[:40],
            "token": _issue(store, msg, choice, "select", agent_page, extra),
        })
    if switch_page:
        buttons.append({
            "text": "上一页",
            "token": _issue(store, msg, session, "switch", agent_page, {**extra, "switch_page": switch_page - 1}),
        })
    if switch_page + 1 < pages:
        buttons.append({
            "text": "下一页",
            "token": _issue(store, msg, session, "switch", agent_page, {**extra, "switch_page": switch_page + 1}),
        })
    buttons.append({"text": "新建会话", "token": _issue(store, msg, session, "new", agent_page, extra)})
    buttons.append({"text": "↩️返回列表", "token": _issue(store, msg, session, "back", agent_page, {"agent_page": agent_page})})
    await _emit(msg, port, f"切换会话 {switch_page + 1}/{pages}。点选或新建会绑定；返回列表不会。", buttons)


async def handle_workers_command(msg: InboundMessage, port: Any, runner: Any, settings: Any, arg: str) -> None:
    """List or leave Workers. Does not bind a session, except ``exit`` clears the binding."""
    if msg.channel not in ("telegram", "feishu"):
        await port.reply(msg, "在 Telegram 或飞书里发送 /workers。Web 控制台可以直接选择会话。")
        return
    if not enabled(settings):
        await port.reply(msg, "Agent 功能未开启：设置 CONVEYOR_AGENTS_ENABLED=true 后重启服务。")
        return
    store = WorkerSessionStore(settings)
    if (arg or "").strip() == "exit":
        store.clear(msg.channel, msg.chat_id, msg.operator_id)
        await port.reply(msg, "已退出 Workers。这个聊天回到原来的对话，Workers 里的历史还在。")
        return
    try:
        page = int(arg) - 1 if (arg or "").strip().isdigit() else 0
    except ValueError:
        page = 0
    await _render_list(msg, port, settings, page)


def _canonical(msg: InboundMessage, session: dict[str, Any], text: str) -> InboundMessage:
    return replace(
        msg,
        channel=session["channel"],  # type: ignore[arg-type]
        operator_id=session["operator_id"],
        chat_id=session["source_chat_id"],
        text=text,
    )


async def handle_workers_token(msg: InboundMessage, port: Any, settings: Any, runner: Any, token: str) -> None:
    """Act on a ``wk:`` token after the caller has authenticated the operator."""
    if len(token.encode("utf-8")) > 60 or not token:
        await port.reply(msg, "这个按钮已失效。")
        return
    store = WorkerSessionStore(settings)
    try:
        found = store.lookup_token(
            token,
            operator_id=msg.operator_id,
            channel=msg.channel,
            topic=_source(store, msg),
        )
        session = store.get(found["session_id"])
        if session is None:
            raise AgentError("callback token rejected")
    except AgentError:
        await port.reply(msg, "这个按钮已失效。")
        return
    action = str(found["action"])
    page = int(found["page"])
    origin = PhysicalOrigin(msg.channel, msg.operator_id, _source(store, msg), msg.chat_type, msg.message_id or "")
    wrapped = PhysicalOriginPort(port, origin, settings)
    if action in _GATED:
        try:
            current = store.selected(msg.channel, msg.chat_id, msg.operator_id)
        except AgentError:
            current = {"session_id": ""}
        if current is not None and current["session_id"] != session["session_id"]:
            await port.reply(msg, "当前已切换到其他会话。切回原来的会话后再确认。")
            return
        canonical = _canonical(msg, session, "")
        native = str((found.get("extra") or {}).get("native") or "")
        if action == "deep":
            from handlers.chat import handle_deep
            await handle_deep(canonical, wrapped, runner, settings=settings)
            return
        from handlers.tools.runner import cancel_pending, execute_confirmed
        tool_token = native.split(":", 2)[-1] if ":" in native else native
        if action == "confirm":
            await execute_confirmed(canonical, wrapped, settings, tool_token)
        else:
            await cancel_pending(canonical, wrapped, settings, tool_token)
        return
    if action in _JOB:
        from handlers.conversation_jobs import handle_conversation_command
        canonical = _canonical(msg, session, f"/{action}")
        handled = await handle_conversation_command(action, canonical, wrapped, runner, settings, "")
        if not handled:
            await port.reply(msg, "此会话没有可操作的任务。")
        return
    if action == "exit":
        store.clear(msg.channel, msg.chat_id, msg.operator_id)
        await port.reply(msg, "已退出 Workers。这个聊天回到原来的对话，Workers 里的历史还在。")
        return
    if action == "continue" or action == "select":
        store.select(msg.channel, msg.chat_id, msg.operator_id, session["session_id"])
        await port.reply(msg, f"已在这个聊天继续「{session['title']}」。之后的消息进入该会话；应用或丢弃改动需要单独确认。")
        return
    if action == "new":
        created = store.create(session["agent_id"])
        store.select(msg.channel, msg.chat_id, msg.operator_id, created["session_id"])
        await port.reply(msg, f"已新建并绑定「{created['title']}」。")
        return
    if action == "open":
        await _render_detail(msg, port, settings, session, page)
        return
    if action == "tasks":
        jobs = _live_jobs(settings, session)
        lines = [f"{session['title']} 的任务"]
        lines.extend(f"{job['id']} · {job['state']} · {job['prompt_preview']}" for job in jobs)
        buttons = [
            {"text": label, "token": _issue(store, msg, session, name, page, {"agent_page": page})}
            for label, name in (("状态", "status"), ("取消", "cancel"), ("差异", "diff"), ("应用", "apply"), ("丢弃", "discard"))
        ]
        buttons.append({"text": "↩️返回", "token": _issue(store, msg, session, "open", page, {"agent_page": page})})
        await _emit(msg, port, "\n".join(lines) if jobs else lines[0] + "\n此会话还没有任务。", buttons)
        return
    if action == "switch":
        extra = found.get("extra") or {}
        agent_page = int(extra.get("agent_page") if extra.get("agent_page") is not None else page)
        switch_page = int(extra.get("switch_page") or 0)
        await _render_switch(msg, port, settings, session, agent_page, switch_page)
        return
    if action in ("list", "back"):
        await _render_list(msg, port, settings, page)
        return
    await port.reply(msg, "这个按钮已失效。")
