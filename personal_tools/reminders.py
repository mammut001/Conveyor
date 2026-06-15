"""personal_tools/reminders.py — local reminder tool implementations."""
from __future__ import annotations

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.reminder_parse import REMIND_USAGE, parse_reminder_text
from personal_tools.store import PersonalToolsStore


def _format_reminder(row) -> str:
    flag = {"pending": "⏳", "cancelled": "❌", "done": "✅"}.get(row.status, "?")
    preview = row.text.replace("\n", " ")
    if len(preview) > 100:
        preview = preview[:97] + "..."
    return f"{flag} #{row.id} · {row.due_at} · {preview}"


async def reminders_create(settings: Settings, arg: str, *, operator_id: str) -> ToolResult:
    parsed = parse_reminder_text(arg, tz_name=settings.user_timezone)
    if parsed is None:
        return ToolResult(False, REMIND_USAGE)
    body, due_at = parsed
    if not body:
        return ToolResult(False, REMIND_USAGE)
    store = PersonalToolsStore(settings)
    row = store.create_reminder(operator_id, body, due_at)
    return ToolResult(True, f"提醒已创建 #{row.id} · {row.due_at} · {body}")


async def reminders_list(settings: Settings, arg: str, *, operator_id: str) -> ToolResult:
    limit = 20
    raw = (arg or "").strip()
    if raw:
        try:
            limit = max(1, min(50, int(raw)))
        except ValueError:
            return ToolResult(False, "用法: /reminders [条数]")
    store = PersonalToolsStore(settings)
    rows = store.list_reminders(operator_id, limit=limit)
    if not rows:
        return ToolResult(True, "没有提醒。用 /remind 创建。")
    lines = [f"提醒列表 ({len(rows)}):", *(_format_reminder(r) for r in rows)]
    return ToolResult(True, "\n".join(lines))


async def reminders_cancel(settings: Settings, arg: str, *, operator_id: str) -> ToolResult:
    raw = (arg or "").strip()
    if not raw:
        return ToolResult(False, "用法: reminders.cancel <id>")
    try:
        reminder_id = int(raw)
    except ValueError:
        return ToolResult(False, f"无效提醒 id: {raw!r}")
    store = PersonalToolsStore(settings)
    if store.cancel_reminder(operator_id, reminder_id):
        return ToolResult(True, f"已取消提醒 #{reminder_id}")
    return ToolResult(False, f"提醒 #{reminder_id} 不存在或已取消。")


async def reminders_due(settings: Settings, arg: str, *, operator_id: str) -> ToolResult:
    _ = arg
    store = PersonalToolsStore(settings)
    rows = store.list_due_reminders(operator_id)
    if not rows:
        return ToolResult(True, "当前没有到期的待办提醒。")
    lines = [f"到期提醒 ({len(rows)}):", *(_format_reminder(r) for r in rows)]
    return ToolResult(True, "\n".join(lines))
