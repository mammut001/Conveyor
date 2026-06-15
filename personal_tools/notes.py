"""personal_tools/notes.py — local notes tool implementations."""
from __future__ import annotations

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.store import PersonalToolsStore


def _format_note(row) -> str:
    preview = row.text.replace("\n", " ")
    if len(preview) > 120:
        preview = preview[:117] + "..."
    return f"#{row.id} · {row.created_at} · {preview}"


async def notes_add(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    text = (arg or "").strip()
    if not text:
        return ToolResult(False, "用法: /note <内容>")
    store = PersonalToolsStore(settings)
    row = store.add_note(operator_id, text)
    return ToolResult(True, f"笔记已保存 #{row.id}")


async def notes_search(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    query = (arg or "").strip()
    if not query:
        return ToolResult(False, "用法: /notes <关键词>")
    store = PersonalToolsStore(settings)
    rows = store.search_notes(operator_id, query)
    if not rows:
        return ToolResult(True, f"没有匹配「{query}」的笔记。")
    lines = [f"找到 {len(rows)} 条笔记:", *(_format_note(r) for r in rows)]
    return ToolResult(True, "\n".join(lines))


async def notes_list_recent(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    limit = 10
    raw = (arg or "").strip()
    if raw:
        try:
            limit = max(1, min(50, int(raw)))
        except ValueError:
            return ToolResult(False, "用法: /notes [条数|关键词]")
    store = PersonalToolsStore(settings)
    rows = store.list_recent_notes(operator_id, limit=limit)
    if not rows:
        return ToolResult(True, "还没有笔记。用 /note <内容> 添加。")
    lines = [f"最近 {len(rows)} 条笔记:", *(_format_note(r) for r in rows)]
    return ToolResult(True, "\n".join(lines))


async def notes_delete(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    raw = (arg or "").strip()
    if not raw:
        return ToolResult(False, "用法: notes.delete <id>")
    try:
        note_id = int(raw)
    except ValueError:
        return ToolResult(False, f"无效笔记 id: {raw!r}")
    store = PersonalToolsStore(settings)
    if store.delete_note(operator_id, note_id):
        return ToolResult(True, f"已删除笔记 #{note_id}")
    return ToolResult(False, f"笔记 #{note_id} 不存在。")
