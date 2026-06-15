"""personal_tools/registry.py — personal tool registry and execution."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from config import Settings
from handlers.tools.registry import DangerLevel
from personal_tools.base import PersonalToolSpec, ToolResult
from personal_tools import notes as notes_tools
from personal_tools import reminders as reminders_tools

if TYPE_CHECKING:
    pass

PersonalExecutor = Callable[..., Awaitable[ToolResult]]

PERSONAL_TOOL_REGISTRY: dict[str, PersonalToolSpec] = {}
_PERSONAL_EXECUTORS: dict[str, PersonalExecutor] = {}


def _register(
    name: str,
    summary: str,
    danger: DangerLevel,
    executor: PersonalExecutor,
    *,
    keywords: tuple[str, ...] = (),
) -> None:
    PERSONAL_TOOL_REGISTRY[name] = PersonalToolSpec(
        name=name,
        summary=summary,
        danger=danger,
        keywords=keywords,
    )
    _PERSONAL_EXECUTORS[name] = executor


def register_personal_tools() -> None:
    if PERSONAL_TOOL_REGISTRY:
        return
    _register(
        "notes.add",
        "添加本地笔记",
        DangerLevel.WRITE_SAFE,
        notes_tools.notes_add,
        keywords=("笔记", "note"),
    )
    _register(
        "notes.search",
        "搜索本地笔记",
        DangerLevel.READ,
        notes_tools.notes_search,
    )
    _register(
        "notes.list_recent",
        "列出最近笔记",
        DangerLevel.READ,
        notes_tools.notes_list_recent,
    )
    _register(
        "notes.delete",
        "删除本地笔记 (需确认)",
        DangerLevel.DESTRUCTIVE,
        notes_tools.notes_delete,
    )
    _register(
        "reminders.create",
        "创建本地提醒",
        DangerLevel.WRITE_SAFE,
        reminders_tools.reminders_create,
        keywords=("提醒", "remind"),
    )
    _register(
        "reminders.list",
        "列出提醒",
        DangerLevel.READ,
        reminders_tools.reminders_list,
    )
    _register(
        "reminders.cancel",
        "取消提醒 (需确认)",
        DangerLevel.WRITE,
        reminders_tools.reminders_cancel,
    )
    _register(
        "reminders.due",
        "列出到期提醒",
        DangerLevel.READ,
        reminders_tools.reminders_due,
    )


def get_personal_tool(name: str) -> PersonalToolSpec | None:
    register_personal_tools()
    return PERSONAL_TOOL_REGISTRY.get(name)


def requires_personal_confirmation(name: str) -> bool:
    spec = get_personal_tool(name)
    if spec is None:
        return False
    return spec.danger in (DangerLevel.WRITE, DangerLevel.DESTRUCTIVE)


async def execute_personal_tool(
    settings: Settings,
    tool_name: str,
    arg: str,
    *,
    operator_id: str,
) -> str:
    register_personal_tools()
    executor = _PERSONAL_EXECUTORS.get(tool_name)
    if executor is None:
        return f"未知个人工具: {tool_name}"
    try:
        result: ToolResult = await executor(settings, arg, operator_id=operator_id)
    except Exception as exc:
        return f"个人工具 {tool_name} 执行失败: {type(exc).__name__}"
    return result.text


def personal_tool_danger(name: str) -> str:
    spec = get_personal_tool(name)
    return spec.danger.value if spec else "unknown"
