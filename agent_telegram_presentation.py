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
    """Produce a concise, human-readable Chinese capability summary."""
    if not tools_subset:
        return "无额外外部工具（仅对话分析）"
    labels: list[str] = []
    for tool_name in tools_subset:
        if tool_name in _TOOL_LABELS:
            labels.append(_TOOL_LABELS[tool_name])
        else:
            try:
                from handlers.tools.registry import get_tool
                spec = get_tool(tool_name)
                if spec and getattr(spec, "summary", ""):
                    labels.append(f"{tool_name} ({spec.summary})")
                else:
                    labels.append(tool_name)
            except Exception:
                labels.append(tool_name)
    return "、".join(labels)


def format_route_confirmation_card(settings: Settings, arg: str) -> str:
    """Generate human-readable Chinese confirmation card for main.route_delegate on Telegram."""
    import agent_delegation
    req = agent_delegation.parse_request(arg)
    target_id = req["to"]
    from agents import AgentStore
    agent = AgentStore(settings).get(target_id)
    agent_name = agent["name"] if agent else target_id

    raw_task = str(req.get("task", "")).strip()
    redacted_task = truncate(redact_text(raw_task), 300)
    capabilities = _summarize_capabilities(settings, req.get("tools_subset", []))

    return (
        f"📋 任务转交确认\n\n"
        f"目标 Agent: {agent_name}\n"
        f"任务内容: {redacted_task}\n"
        f"所需权限: {capabilities}\n\n"
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

        text = (
            f"已交给 {agent_name}，正在排队。完成后会在这里通知你。\n"
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

    # Truthful generalized failure (never turn errors into success)
    return f"转交未执行：{trimmed}", None


def query_delegated_task_status(
    settings: Settings,
    msg: InboundMessage,
    task_ref: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Owner-scoped read-only query of actual mailbox status and route binding.

    Guarantees:
    - Never queries ordinary job queue (different tasks).
    - Enforces chat_type=='p2p', origin_channel, origin_chat, origin_operator, allowlist.
    - No forged cross-owner or cross-chat access.
    - Revalidates kill policy and archived agent status.
    - Never returns task bodies or secret args.
    """
    ref = str(task_ref or "").strip()
    if not ref or not re.fullmatch(r"[0-9a-zA-Z_-]{6,64}", ref):
        return None, "任务编号格式无效。"

    from channel.auth import is_allowed
    import agent_channel_routing as routing

    if not is_allowed(msg, settings) or msg.chat_type != "p2p":
        return None, "未找到该任务或无权查看。"

    owner = routing._owner(settings, msg.channel)
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

        rows = conn.execute(
            """SELECT m.id, m.status, m.attempts, m.max_attempts, m.deadline,
                      m.lease_expires_at, m.created_at, r.agent_id, r.agent_name,
                      r.origin_channel, r.origin_operator, r.origin_chat, r.delivered
               FROM agent_mailbox m
               JOIN main_router_routes r ON r.approval_key = m.idempotency_key
               WHERE (m.id = ? OR m.id LIKE ? || '%') AND m.kind = 'task'
               ORDER BY m.created_at DESC LIMIT 2""",
            (ref, ref),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None, "未找到该任务或无权查看。"
    if len(rows) > 1 and rows[0]["id"] != ref:
        return None, "任务编号前缀不唯一，请提供更完整的编号。"

    row = dict(rows[0])

    # Revalidate exact ownership and channel routing
    if (
        row["origin_channel"] != msg.channel
        or str(row["origin_chat"]) != str(msg.chat_id)
        or str(row["origin_operator"]) != str(msg.operator_id)
    ):
        return None, "未找到该任务或无权查看。"

    import agent_security
    if agent_security.killed(settings):
        return {
            "agent_name": row["agent_name"],
            "task_id": row["id"],
            "state": "cancelled",
            "state_label": "已终止 (kill switch 激活)",
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "is_terminal": True,
            "detail": "紧急停止开关已激活，任务已停止处理。",
        }, None

    from agents import AgentStore
    store = AgentStore(settings)
    target_agent = store.get(row["agent_id"])
    if target_agent is None or target_agent.get("archived", False):
        return {
            "agent_name": row["agent_name"],
            "task_id": row["id"],
            "state": "failed",
            "state_label": "代理不可用 (Agent 已归档)",
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "is_terminal": True,
            "detail": "目标 Agent 已被归档或不存在。",
        }, None

    now = time.time()
    status = row["status"]
    attempts = row["attempts"]
    max_attempts = row["max_attempts"]
    deadline = row["deadline"]
    lease_expires = row["lease_expires_at"]

    if status == "done":
        state = "done"
        state_label = "已完成 (done)"
        is_terminal = True
    elif status == "queued":
        if deadline and now >= deadline:
            state = "expired"
            state_label = "已过期 (expired)"
            is_terminal = True
        else:
            state = "queued"
            state_label = "正在排队 (queued)"
            is_terminal = False
    elif status == "leased":
        if deadline and now >= deadline:
            state = "expired"
            state_label = "已过期 (expired)"
            is_terminal = True
        elif lease_expires and now < lease_expires:
            state = "running"
            state_label = "处理中 (running)"
            is_terminal = False
        else:
            if attempts < max_attempts:
                state = "retrying"
                state_label = "等待重试 (retrying)"
                is_terminal = False
            else:
                state = "failed"
                state_label = "已失败 (failed)"
                is_terminal = True
    elif status == "failed":
        if attempts < max_attempts and (not deadline or now < deadline):
            state = "retrying"
            state_label = "等待重试 (retrying)"
            is_terminal = False
        else:
            state = "failed"
            state_label = "已失败 (failed)"
            is_terminal = True
    elif status == "dead":
        if deadline and now >= deadline:
            state = "expired"
            state_label = "已过期 (expired)"
        elif attempts >= max_attempts:
            state = "failed"
            state_label = "已失败 (failed)"
        else:
            state = "cancelled"
            state_label = "已取消 (cancelled)"
        is_terminal = True
    else:
        state = "unknown"
        state_label = "未知状态"
        is_terminal = True

    return {
        "agent_name": row["agent_name"],
        "task_id": row["id"],
        "state": state,
        "state_label": state_label,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "is_terminal": is_terminal,
        "detail": None,
    }, None


def get_task_status_display(
    settings: Settings,
    msg: InboundMessage,
    task_ref: str,
) -> tuple[str, list[list[dict[str, str]]] | None]:
    """Return formatted status text and optional refresh button."""
    info, error = query_delegated_task_status(settings, msg, task_ref)
    if error is not None or info is None:
        return error or "未找到该任务或无权查看。", None

    text = (
        f"📊 任务状态\n\n"
        f"目标 Agent: {info['agent_name']}\n"
        f"任务编号: {info['task_id'][:12]}\n"
        f"当前状态: {info['state_label']}\n"
        f"执行进度: 第 {info['attempts']}/{info['max_attempts']} 次"
    )
    if info.get("detail"):
        text += f"\n说明: {info['detail']}"
    elif info["state"] == "done":
        text += "\n结果已在此会话中发送。"
    elif not info["is_terminal"]:
        text += "\n完成后会在这里通知你。"

    buttons = None
    if not info["is_terminal"]:
        buttons = [[
            {"text": "🔄 刷新状态", "callback_data": f"task:status:{info['task_id']}"}
        ]]
    return text, buttons


def convert_markdown_tables_to_bullets(text: str) -> str:
    """Convert common simple Markdown tables into readable plain text bullet rows.

    Preserves:
    - Code blocks (fenced with ``` or ~~~) completely intact
    - Non-table text, headers, lists, code spans
    - No HTML injection
    """
    if not text or "|" not in text:
        return text

    lines = text.split("\n")
    out_lines: list[str] = []
    in_code_block = False
    code_fence = ""

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Handle fenced code block toggling
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            if not in_code_block:
                in_code_block = True
                code_fence = fence
                out_lines.append(line)
                i += 1
                continue
            elif fence == code_fence:
                in_code_block = False
                code_fence = ""
                out_lines.append(line)
                i += 1
                continue

        if in_code_block:
            out_lines.append(line)
            i += 1
            continue

        # Check if line i could be table header and line i+1 separator
        if "|" in line and i + 1 < n and _MD_SEP_PATTERN.match(lines[i + 1]):
            header_line = line
            separator_line = lines[i + 1]

            def parse_cells(row_str: str) -> list[str]:
                s = row_str.strip()
                if s.startswith("|"):
                    s = s[1:]
                if s.endswith("|"):
                    s = s[:-1]
                return [c.strip() for c in s.split("|")]

            headers = parse_cells(header_line)
            # Find data lines
            data_rows: list[list[str]] = []
            j = i + 2
            while j < n:
                d_line = lines[j]
                d_stripped = d_line.strip()
                if not d_stripped or "|" not in d_line:
                    break
                # If a new code fence or separator appears, stop
                if d_stripped.startswith("```") or d_stripped.startswith("~~~"):
                    break
                data_rows.append(parse_cells(d_line))
                j += 1

            if data_rows:
                # Convert table rows to bullet rows
                bullet_lines: list[str] = []
                for row_cells in data_rows:
                    if not any(row_cells):
                        continue
                    if len(headers) == 2:
                        col0 = row_cells[0] if len(row_cells) > 0 else ""
                        col1 = row_cells[1] if len(row_cells) > 1 else ""
                        if col0 and col1:
                            bullet_lines.append(f"• {col0}: {col1}")
                        elif col0:
                            bullet_lines.append(f"• {col0}")
                        elif col1:
                            bullet_lines.append(f"• {col1}")
                    elif len(headers) >= 3:
                        col0 = row_cells[0] if len(row_cells) > 0 else ""
                        pairs = []
                        for h_idx in range(1, len(headers)):
                            h_name = headers[h_idx]
                            v_val = row_cells[h_idx] if h_idx < len(row_cells) else ""
                            if v_val:
                                pairs.append(f"{h_name}: {v_val}")
                        if col0 and pairs:
                            bullet_lines.append(f"• {col0} — " + ", ".join(pairs))
                        elif col0:
                            bullet_lines.append(f"• {col0}")
                        elif pairs:
                            bullet_lines.append("• " + ", ".join(pairs))
                    else:
                        bullet_lines.append("• " + " | ".join(c for c in row_cells if c))

                out_lines.extend(bullet_lines)
                i = j
                continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines)


def format_telegram_result(agent_name: str, result_tag: str, result_text: str) -> str:
    """Format Telegram-safe completed result notification with localized header and stable tag."""
    redacted = redact_text(result_text or "")
    formatted_body = convert_markdown_tables_to_bullets(redacted)[:2400]
    return f"[{agent_name}] 已完成 [result:{result_tag}]:\n{formatted_body}"
