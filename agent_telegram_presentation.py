"""Modular presentation helpers for Telegram agent delegation UX.

Translates internal machine-readable contracts to user-facing Chinese
presentation boundaries while preserving:
- Exact approval payload and bindings
- Underlying 'agent task queued: <uuid>' backend contracts
- Stable result tags [result:<tag>]
- Untrusted authority metadata in internal transcripts
- Safe, non-destructive status inspection
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

from channel.types import InboundMessage
from config import Settings
from redaction import redact_text, truncate

log = logging.getLogger(__name__)

# Capability cues to human-readable Chinese capability labels
_TOOL_LABELS: dict[str, str] = {
    "notes.search": "笔记搜索 (notes.search)",
    "calendar.search": "日历查询 (calendar.search)",
    "github.create_issue": "创建 GitHub Issue (github.create_issue)",
}

# Known backend rejection strings mapped to clear Chinese failure reasons
_REJECTION_MAP: dict[str, str] = {
    "main route denied": "转交失败：请求被拒绝或权限不足。",
    "main route disabled": "转交未开启：主路由功能已停用。",
    "agent delegation denied": "转交失败：代理委托被拒绝（可能超出频率限制或目标已变更）。",
    "human approval required": "转交失败：需要人工审批确认。",
    "agent tools disabled by emergency kill switch": "转交阻止：紧急停止开关已激活，代理工具已禁用。",
    "agent tool denied": "转交失败：无权使用该代理工具。",
    "unknown tool": "转交失败：未知工具。",
}

_MD_SEP_PATTERN = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_CODE_FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def format_safe_single_line(text: str, max_chars: int = 30, ellipsis: str = "…") -> str:
    """Narrow presentation helper for single-line redacted bounded names/labels.

    Normalizes embedded newlines and control characters to spaces, redacts secrets,
    and cleanly truncates with an ellipsis without adding newlines or reserving 80 chars.
    """
    if not text:
        return ""
    redacted = redact_text(str(text))
    cleaned = _CONTROL_CHARS_PATTERN.sub(" ", redacted)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= len(ellipsis):
        return cleaned[:max_chars]
    return cleaned[: max_chars - len(ellipsis)].rstrip() + ellipsis



def feature_enabled(settings: Settings) -> bool:
    """Return True if both channel routing and main router features are enabled."""
    import agent_channel_routing as routing
    import main_router
    return bool(routing.enabled(settings) and main_router.enabled(settings))


def validate_route_target(settings: Settings, arg: str) -> bool:
    """Validate target agent exists and is active; fail-closed on invalid/archived."""
    try:
        import agent_delegation
        req = agent_delegation.parse_request(arg)
        target_id = req.get("to")
        if not target_id or not isinstance(target_id, str):
            return False
        from agents import AgentStore
        agent = AgentStore(settings).get(target_id)
        if agent is None or agent.get("archived", False):
            return False
        return True
    except Exception:
        return False


def _summarize_capabilities(settings: Settings, tools_subset: Sequence[str]) -> str:
    """Produce a concise, human-readable Chinese capability summary with bounded labels."""
    if not tools_subset:
        return "无额外外部工具（仅对话分析）"
    labels: list[str] = []
    for tool_name in tools_subset:
        tool_clean = truncate(redact_text(str(tool_name or "")), 50)
        if tool_name in _TOOL_LABELS:
            labels.append(_TOOL_LABELS[tool_name])
        else:
            try:
                from handlers.tools.registry import get_tool
                spec = get_tool(tool_name)
                if spec and getattr(spec, "summary", ""):
                    summary_clean = truncate(redact_text(str(spec.summary)), 50)
                    labels.append(f"{tool_clean} ({summary_clean})")
                else:
                    labels.append(tool_clean)
            except Exception:
                labels.append(tool_clean)
    return "、".join(labels)


def format_route_confirmation_card(settings: Settings, arg: str) -> str:
    """Generate human-readable Chinese confirmation card for main.route_delegate on Telegram."""
    import agent_delegation
    req = agent_delegation.parse_request(arg)
    target_id = req["to"]
    from agents import AgentStore
    agent = AgentStore(settings).get(target_id)
    raw_name = agent["name"] if agent else target_id
    safe_agent_name = format_safe_single_line(raw_name, 40)

    raw_task = str(req.get("task", "")).strip()
    safe_task = truncate(redact_text(raw_task), 300)
    capabilities = _summarize_capabilities(settings, req.get("tools_subset", []))
    safe_capabilities = truncate(capabilities, 120)

    return (
        f"📋 任务转交确认\n\n"
        f"目标助手: {safe_agent_name}\n"
        f"任务内容: {safe_task}\n"
        f"所需权限: {safe_capabilities}\n\n"
        f"确认转交？"
    )


def format_queue_ack(
    settings: Settings,
    msg: InboundMessage,
    action: Any,
    raw_result: str,
) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Translate raw tool result into Chinese queue ack or mapped failure.

    Keeps the backend contract intact while presenting clear, truthful feedback.
    Never states running when merely queued.
    """
    if raw_result.startswith("agent task queued: "):
        task_id = raw_result[len("agent task queued: "):].strip()
        short_ref = task_id[:12]

        agent_name = "Agent"
        try:
            import agent_delegation
            req = agent_delegation.parse_request(action.arg)
            from agents import AgentStore
            agent = AgentStore(settings).get(req["to"])
            if agent:
                agent_name = agent["name"]
        except Exception:
            pass

        safe_name = format_safe_single_line(agent_name, 40)
        text = (
            f"已交给 {safe_name}，正在排队。完成后会在这里通知你。\n"
            f"任务编号: {short_ref}\n"
            f"（可点击下方按钮或发送 /agentstatus {short_ref} 查看进度）"
        )
        buttons = [[
            {"text": "🔍 查看状态", "callback_data": f"task:status:{task_id}"}
        ]]
        return text, buttons

    # Refusal / failure mappings
    trimmed = raw_result.strip()
    if trimmed in _REJECTION_MAP:
        return _REJECTION_MAP[trimmed], None

    for prefix, mapped in _REJECTION_MAP.items():
        if trimmed.startswith(prefix):
            return mapped, None

    # Safe generic failure without exposing private raw payload
    return "转交未执行：系统拒绝或处理异常。", None


def query_delegated_task_status(
    settings: Settings,
    msg: InboundMessage,
    task_ref: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Owner-scoped read-only query of actual mailbox status and route binding.

    Guarantees:
    - Fails closed immediately if feature flags are disabled without touching SQLite.
    - Scopes SQL by origin_channel, origin_chat, origin_operator BEFORE prefix limit/ambiguity.
    - Restricts task_ref to lowercase hex characters [0-9a-f]{6,32}.
    - Verifies m.to_agent == r.agent_id.
    - Inspects agent_result_notifications outbox state for truthful delivery acknowledgment.
    - Preserves historical completed tasks even if agent archived.
    - Distinguishes waiting retry from failed when retry attempts are exhausted.
    - Does not guess cancellation reasons for generic dead tasks.
    - Never returns task bodies or secret args.
    """
    if not feature_enabled(settings):
        return None, "功能未启用或当前不可用。"

    ref = str(task_ref or "").strip().lower()
    if not ref or not re.fullmatch(r"[0-9a-f]{6,32}", ref):
        return None, "任务编号格式无效。"

    from channel.auth import is_allowed
    import agent_channel_routing as routing

    if not is_allowed(msg, settings) or msg.chat_type != "p2p" or msg.channel != "telegram":
        return None, "未找到该任务或无权查看。"

    owner = routing._owner(settings, "telegram")
    if not owner or str(msg.operator_id) != owner:
        return None, "未找到该任务或无权查看。"

    db_path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    if not db_path.exists():
        return None, "未找到该任务或无权查看。"

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "agent_mailbox" not in tables or "main_router_routes" not in tables:
            return None, "未找到该任务或无权查看。"

        # Scope SQL strictly by origin_channel, origin_chat, origin_operator BEFORE LIMIT
        rows = conn.execute(
            """SELECT m.id, m.status, m.attempts, m.max_attempts, m.deadline,
                      m.lease_expires_at, m.created_at, r.agent_id, r.agent_name,
                      r.origin_channel, r.origin_operator, r.origin_chat, r.delivered
               FROM agent_mailbox m
               JOIN main_router_routes r ON r.approval_key = m.idempotency_key
               WHERE r.origin_channel = ? AND r.origin_chat = ? AND r.origin_operator = ?
                 AND m.kind = 'task' AND m.to_agent = r.agent_id
                 AND (m.id = ? OR m.id LIKE ? || '%')
               ORDER BY m.created_at DESC LIMIT 2""",
            (msg.channel, str(msg.chat_id), str(msg.operator_id), ref, ref),
        ).fetchall()

        if not rows:
            return None, "未找到该任务或无权查看。"
        if len(rows) > 1 and rows[0]["id"] != ref:
            return None, "任务编号前缀不唯一，请提供更完整的编号。"

        row = dict(rows[0])
        task_id = row["id"]

        # Check outbox notification state if table exists
        outbox_state = None
        if "agent_result_notifications" in tables:
            outbox_row = conn.execute(
                """SELECT state FROM agent_result_notifications
                   WHERE task_id = ? AND channel = ? AND chat_id = ? AND operator = ?
                   LIMIT 1""",
                (task_id, msg.channel, str(msg.chat_id), str(msg.operator_id)),
            ).fetchone()
            if outbox_row:
                outbox_state = outbox_row["state"]
    finally:
        conn.close()

    now = time.time()
    from agents import AgentStore
    store = AgentStore(settings)
    target_agent = store.get(row["agent_id"])
    is_archived = target_agent is None or bool(target_agent.get("archived", False))

    state_label, detail, is_terminal = _interpret_task_lifecycle(
        dict(row), outbox_state, is_archived, now
    )

    safe_name = format_safe_single_line(row["agent_name"], 40)
    return {
        "agent_name": safe_name,
        "task_id": task_id,
        "state_label": state_label,
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "is_terminal": is_terminal,
        "detail": detail,
    }, None


def _format_time(created_at: float | int | None, tz_name: str | None) -> str:
    """Format timestamp into readable user timezone string; gracefully fallback to UTC."""
    if not created_at:
        return ""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        from datetime import timezone
        tz = timezone.utc
    try:
        from datetime import datetime
        dt = datetime.fromtimestamp(float(created_at), tz=tz)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _interpret_task_lifecycle(
    row: dict[str, Any],
    outbox_state: str | None,
    is_archived: bool,
    now: float,
) -> tuple[str, str | None, bool]:
    """Shared truthful state interpretation for mailbox tasks avoiding lifecycle drift."""
    status = row.get("status")
    attempts = row.get("attempts", 0)
    max_attempts = row.get("max_attempts", 1)
    deadline = row.get("deadline")
    lease_expires = row.get("lease_expires_at")

    detail = None
    is_terminal = False

    # 1. Historical completed task: NEVER rewrite done -> failed/cancelled
    if status == "done":
        state_label = "已完成"
        if outbox_state == "sent":
            detail = "结果已发送。"
            is_terminal = True
        elif outbox_state in ("pending", "leased", "retry"):
            detail = "结果已生成，正在等待发送。"
            # Allow user to refresh while delivery is unresolved
            is_terminal = False
        elif outbox_state == "dead":
            detail = "结果已生成，但通知发送未成功。"
            is_terminal = True
        else:
            detail = "结果已生成。"
            is_terminal = True

    # 2. Terminal dead task: do not guess cancelled vs revoked; use generic stopped
    elif status == "dead":
        if deadline and now >= deadline:
            state_label = "已超时"
        else:
            state_label = "已停止"
        is_terminal = True

    # 3. Active tasks subject to archived agent policy
    elif is_archived:
        state_label = "助手不可用"
        detail = "目标助手已被归档或不可用。"
        is_terminal = True

    # 4. Normal active lifecycle states
    elif status == "queued":
        if deadline and now >= deadline:
            state_label = "已超时"
            is_terminal = True
        else:
            state_label = "正在排队"
            is_terminal = False
    elif status == "leased":
        if deadline and now >= deadline:
            state_label = "已超时"
            is_terminal = True
        elif lease_expires and now < lease_expires:
            state_label = "处理中"
            is_terminal = False
        else:
            # Lease expired
            if attempts >= max_attempts:
                state_label = "已失败"
                is_terminal = True
            else:
                state_label = "等待重试"
                is_terminal = False
    elif status == "failed":
        if deadline and now >= deadline:
            state_label = "已超时"
            is_terminal = True
        elif attempts >= max_attempts:
            state_label = "已失败"
            is_terminal = True
        else:
            state_label = "等待重试"
            is_terminal = False
    else:
        state_label = "状态未知"
        is_terminal = True

    return state_label, detail, is_terminal


def query_recent_delegated_tasks(
    settings: Settings,
    msg: InboundMessage,
    limit: int = 5,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Owner-scoped read-only query of recent delegated tasks for this Telegram chat.

    Guarantees:
    - Fails closed immediately if feature flags are disabled without touching SQLite.
    - Fails closed for unauthorized / non-owner / non-p2p / non-telegram.
    - Scopes SQL by origin_channel, origin_chat, origin_operator BEFORE LIMIT.
    - Requires m.kind = 'task' and m.to_agent = r.agent_id.
    - Deterministic order: m.created_at DESC, m.id DESC LIMIT <limit>.
    - Inspects outbox state for truthful delivery acknowledgment.
    - Preserves historical completed tasks even if agent archived.
    - Never exposes task bodies, payload, or secret args.
    """
    if not feature_enabled(settings):
        return None, "功能未启用或当前不可用。"

    from channel.auth import is_allowed
    import agent_channel_routing as routing

    if not is_allowed(msg, settings) or msg.chat_type != "p2p" or msg.channel != "telegram":
        return None, "未找到任务记录或无权查看。"

    owner = routing._owner(settings, "telegram")
    if not owner or str(msg.operator_id) != owner:
        return None, "未找到任务记录或无权查看。"

    db_path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    if not db_path.exists():
        return [], None

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "agent_mailbox" not in tables or "main_router_routes" not in tables:
            return [], None

        capped_limit = max(1, min(int(limit or 5), 20))
        # Scope SQL strictly by origin_channel, origin_chat, origin_operator BEFORE LIMIT
        rows = conn.execute(
            """SELECT m.id, m.status, m.attempts, m.max_attempts, m.deadline,
                      m.lease_expires_at, m.created_at, r.agent_id, r.agent_name,
                      r.origin_channel, r.origin_operator, r.origin_chat, r.delivered
               FROM agent_mailbox m
               JOIN main_router_routes r ON r.approval_key = m.idempotency_key
               WHERE r.origin_channel = ? AND r.origin_chat = ? AND r.origin_operator = ?
                 AND m.kind = 'task' AND m.to_agent = r.agent_id
               ORDER BY m.created_at DESC, m.id DESC LIMIT ?""",
            (msg.channel, str(msg.chat_id), str(msg.operator_id), capped_limit),
        ).fetchall()

        if not rows:
            return [], None

        task_ids = [r["id"] for r in rows]
        outbox_states: dict[str, str] = {}
        if "agent_result_notifications" in tables and task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            outbox_rows = conn.execute(
                f"""SELECT task_id, state FROM agent_result_notifications
                    WHERE channel = ? AND chat_id = ? AND operator = ?
                      AND task_id IN ({placeholders})""",
                [msg.channel, str(msg.chat_id), str(msg.operator_id)] + task_ids,
            ).fetchall()
            for ob_row in outbox_rows:
                outbox_states[ob_row["task_id"]] = ob_row["state"]
    finally:
        conn.close()

    now = time.time()
    from agents import AgentStore
    store = AgentStore(settings)
    tz_name = getattr(settings, "user_timezone", "UTC") or "UTC"

    tasks = []
    for raw_row in rows:
        row = dict(raw_row)
        task_id = row["id"]
        outbox_state = outbox_states.get(task_id)
        target_agent = store.get(row["agent_id"])
        is_archived = target_agent is None or bool(target_agent.get("archived", False))

        state_label, detail, is_terminal = _interpret_task_lifecycle(
            row, outbox_state, is_archived, now
        )
        safe_name = format_safe_single_line(row["agent_name"], 40)
        created_time = _format_time(row.get("created_at"), tz_name)

        tasks.append({
            "agent_name": safe_name,
            "agent_id": row["agent_id"],
            "task_id": task_id,
            "state_label": state_label,
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "is_terminal": is_terminal,
            "detail": detail,
            "created_time": created_time,
            "created_at": row.get("created_at"),
        })

    return tasks, None


def get_task_status_display(
    settings: Settings,
    msg: InboundMessage,
    task_ref: str,
    *,
    include_back: bool = False,
) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Return formatted status text and optional refresh/back buttons."""
    info, error = query_delegated_task_status(settings, msg, task_ref)
    if error is not None or info is None:
        return error or "未找到该任务或无权查看。", None

    text = (
        f"📊 任务状态\n\n"
        f"目标助手: {info['agent_name']}\n"
        f"任务编号: {info['task_id'][:12]}\n"
        f"状态: {info['state_label']}\n"
        f"执行进度: 第 {info['attempts']}/{info['max_attempts']} 次"
    )
    if info.get("detail"):
        text += f"\n说明: {info['detail']}"
    elif not info["is_terminal"]:
        text += "\n完成后会在这里通知你。"

    buttons: list[list[dict[str, str]]] | None = None
    if include_back:
        row: list[dict[str, str]] = []
        if not info["is_terminal"]:
            row.append({"text": "🔄 刷新状态", "callback_data": f"task:status:{info['task_id']}"})
        row.append({"text": "📋 返回列表", "callback_data": "task:recent"})
        buttons = [row]
    else:
        if not info["is_terminal"]:
            buttons = [[
                {"text": "🔄 刷新状态", "callback_data": f"task:status:{info['task_id']}"}
            ]]
    return text, buttons


def get_recent_tasks_display(
    settings: Settings,
    msg: InboundMessage,
) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Return formatted list of recent delegated tasks with navigation buttons."""
    tasks, error = query_recent_delegated_tasks(settings, msg, limit=5)
    if error is not None:
        return error, None

    if not tasks:
        text = (
            "暂无最近转交任务。\n\n"
            "提示：\n"
            "• 使用默认主助手：发送 /agent main\n"
            "• 发起任务转交：发送 @<助手名> <任务内容>（确认后执行）\n"
            "• 查看与切换助手：发送 /agents"
        )
        buttons = [[{"text": "🔄 刷新列表", "callback_data": "task:recent"}]]
        return text, buttons

    lines = ["📋 最近转交任务（最新 5 条）：\n"]
    buttons: list[list[dict[str, str]]] = []
    for task in tasks:
        short_ref = task["task_id"][:12]
        agent_name = task["agent_name"]
        status_desc = task["state_label"]
        if task.get("detail"):
            status_desc += f"（{task['detail']}）"
        time_desc = f" · {task['created_time']}" if task.get("created_time") else ""
        lines.append(f"• [{agent_name}] {short_ref} — {status_desc}{time_desc}")

        btn_agent = format_safe_single_line(agent_name, 12)
        btn_text = f"🔍 {short_ref} · {btn_agent} ({task['state_label']})"
        btn_text = format_safe_single_line(btn_text, 36)
        buttons.append([{
            "text": btn_text,
            "callback_data": f"task:status:{task['task_id']}",
        }])

    lines.append("\n点击下方按钮可查看详细进度，或发送 /agentstatus <任务编号> 查询。")
    buttons.append([{"text": "🔄 刷新列表", "callback_data": "task:recent"}])
    return "\n".join(lines), buttons


def get_agent_chooser_display(
    settings: Settings,
    msg: InboundMessage,
    page: int = 1,
) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Return Chinese agent chooser display text and compact navigation buttons.

    Guarantees:
    - Fails closed if channel routing disabled or unauthorized.
    - Identifies actual current assistant (fallback actual default name/main when binding unavailable).
    - Lists live non-archived agents, never exposing instructions, paths, credentials, or persona.
    - Paginates up to 50 agents cleanly without silent truncation.
    - Buttons carry exact IDs in callback_data, bounded <= 64 bytes.
    - Includes return-to-main and link to recent tasks (if status feature enabled).
    """
    import agent_channel_routing as routing
    if not routing.enabled(settings):
        return "功能未启用或当前不可用。", None

    if not routing.authorized(settings, msg):
        return "未找到助手或无权操作。", None

    from agents import AgentStore
    store = AgentStore(settings)
    default_agent = store.get("default")
    default_name = default_agent["name"] if default_agent and default_agent.get("name") else "Conveyor"
    safe_default_name = format_safe_single_line(default_name, 20)

    current_agent_id = routing.bound(settings, msg.channel, msg.chat_id)
    if current_agent_id:
        curr = store.get(current_agent_id)
        if curr and not curr.get("archived", False):
            current_name = curr["name"]
            current_ref = curr["id"]
        else:
            current_agent_id = None
            current_name = default_name
            current_ref = "main"
    else:
        current_name = default_name
        current_ref = "main"

    all_agents = [a for a in store.list() if not a.get("archived", False)]
    custom_agents = [a for a in all_agents if a["id"] != "default"]
    custom_agents.sort(key=lambda a: (a.get("name", "").casefold(), a.get("id", "")))

    PAGE_SIZE = 6
    total_custom = len(custom_agents)
    total_pages = max(1, (total_custom + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * PAGE_SIZE
    page_agents = custom_agents[start : start + PAGE_SIZE]

    safe_curr_name = format_safe_single_line(current_name, 30)
    lines = [
        "🤖 助手切换\n",
        f"当前助手: {safe_curr_name} ({current_ref})\n",
    ]
    if total_pages > 1:
        lines.append(f"可选助手（第 {page}/{total_pages} 页，共 {total_custom} 个自定义助手）：")
    else:
        lines.append(f"可选助手（共 {total_custom} 个自定义助手）：")

    is_main_cur = (current_agent_id is None or current_agent_id == "default")
    main_cur_label = "（当前）" if is_main_cur else ""
    lines.append(f"• main ({safe_default_name}) — 默认主助手{main_cur_label}")
    for a in page_agents:
        safe_a_name = format_safe_single_line(a["name"], 30)
        is_cur = (current_agent_id == a["id"])
        cur_label = "（当前）" if is_cur else ""
        lines.append(f"• {safe_a_name} ({a['id']}){cur_label}")

    lines.append("\n点击下方按钮直接切换，或发送 /agent <名称|ID|main>。")
    if total_pages > 1:
        page_hints = []
        if page > 1:
            page_hints.append(f"/agents {page - 1}（上一页）")
        if page < total_pages:
            page_hints.append(f"/agents {page + 1}（下一页）")
        lines.append(f"翻页命令：{' · '.join(page_hints)}")
    if feature_enabled(settings):
        lines.append("发送 /agentstatus 可查看最近转交任务。")

    buttons: list[list[dict[str, str]]] = []
    # Buttons for agents on this page
    for a in page_agents:
        is_cur = (current_agent_id == a["id"])
        mark = "✓ " if is_cur else ""
        id_part = f" ({a['id']})"
        avail_for_name = max(4, 32 - len(mark) - len(id_part))
        safe_btn_name = format_safe_single_line(a["name"], avail_for_name)
        btn_label = f"{mark}{safe_btn_name}{id_part}"
        buttons.append([{
            "text": btn_label,
            "callback_data": f"agent:select:{a['id']}",
        }])

    # Pagination controls if multiple pages
    if total_pages > 1:
        p_row: list[dict[str, str]] = []
        if page > 1:
            p_row.append({"text": "⬅️ 上一页", "callback_data": f"agent:page:{page - 1}"})
        p_row.append({"text": f"{page}/{total_pages}", "callback_data": f"agent:page:{page}"})
        if page < total_pages:
            p_row.append({"text": "下一页 ➡️", "callback_data": f"agent:page:{page + 1}"})
        buttons.append(p_row)

    # Control row: main return + recent tasks
    ctrl_row: list[dict[str, str]] = []
    main_mark = "✓ " if is_main_cur else ""
    ctrl_row.append({
        "text": f"{main_mark}🏠 main ({safe_default_name})",
        "callback_data": "agent:select:default",
    })
    if feature_enabled(settings):
        ctrl_row.append({
            "text": "📋 最近任务",
            "callback_data": "task:recent",
        })
    buttons.append(ctrl_row)

    return "\n".join(lines), buttons


def _parse_table_cells(row_str: str) -> list[str]:
    """Parse pipe-separated cells from a Markdown table row."""
    s = row_str.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_unmistakable_simple_table(
    header_line: str,
    separator_line: str,
    data_lines: list[str],
) -> bool:
    """Strictly validate whether a table block is an unmistakable simple table.

    Rejects:
    - Escaped pipes (\\|)
    - Pipes inside inline code (`...|...`)
    - Separator column count mismatch with headers
    - Row cell counts that do not exactly match header cell count
    - Empty headers
    """
    all_lines = [header_line, separator_line] + data_lines
    for line in all_lines:
        if r"\|" in line:
            return False
        # Check for pipe inside inline backticks `...|...`
        code_spans = re.findall(r"`[^`]*`", line)
        for span in code_spans:
            if "|" in span:
                return False

    headers = _parse_table_cells(header_line)
    if not headers or any(not h for h in headers):
        return False

    header_count = len(headers)
    sep_cells = _parse_table_cells(separator_line)
    if len(sep_cells) != header_count:
        return False

    if not data_lines:
        return False

    for d_line in data_lines:
        cells = _parse_table_cells(d_line)
        if len(cells) != header_count:
            return False

    return True


def convert_markdown_tables_to_bullets(text: str) -> str:
    """Convert only unmistakable simple Markdown tables into readable plain text bullet rows.

    Leaves complex, malformed, unequal-width, code-span, or escaped-pipe tables untouched.
    Preserves fenced code blocks with >= 3 backticks or tildes.
    Preserves column headers and units without dropping data.
    """
    if not text or "|" not in text:
        return text

    lines = text.split("\n")
    out_lines: list[str] = []
    in_code_block = False
    code_fence_str = ""

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        fence_match = _CODE_FENCE_PATTERN.match(line)
        if fence_match:
            fence = fence_match.group(1)
            if not in_code_block:
                in_code_block = True
                code_fence_str = fence
                out_lines.append(line)
                i += 1
                continue
            elif fence[0] == code_fence_str[0] and len(fence) >= len(code_fence_str):
                in_code_block = False
                code_fence_str = ""
                out_lines.append(line)
                i += 1
                continue

        if in_code_block:
            out_lines.append(line)
            i += 1
            continue

        # Check candidate table block
        if "|" in line and i + 1 < n and _MD_SEP_PATTERN.match(lines[i + 1]):
            header_line = line
            separator_line = lines[i + 1]

            data_lines: list[str] = []
            j = i + 2
            while j < n:
                d_line = lines[j]
                if not d_line.strip() or "|" not in d_line:
                    break
                if _CODE_FENCE_PATTERN.match(d_line):
                    break
                data_lines.append(d_line)
                j += 1

            if data_lines and _is_unmistakable_simple_table(header_line, separator_line, data_lines):
                headers = _parse_table_cells(header_line)
                bullet_lines: list[str] = []
                for d_line in data_lines:
                    row_cells = _parse_table_cells(d_line)
                    pairs = [f"{h}：{v}" for h, v in zip(headers, row_cells) if v]
                    if pairs:
                        bullet_lines.append("• " + "；".join(pairs))
                    else:
                        bullet_lines.append("• " + "；".join(row_cells))

                out_lines.extend(bullet_lines)
                i = j
                continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines)


def format_telegram_result(agent_name: str, result_tag: str, result_text: str) -> str:
    """Format Telegram-safe completed result notification with localized header and stable tag."""
    safe_name = format_safe_single_line(agent_name or "", 40)
    redacted = redact_text(result_text or "")
    formatted_body = convert_markdown_tables_to_bullets(redacted)
    full_text = f"[{safe_name}] 已完成 [result:{result_tag}]:\n{formatted_body}"
    return truncate(full_text, 2400)
