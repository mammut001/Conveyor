"""V2 adapters for the existing chat tool registry and confirmation pipeline."""
from __future__ import annotations

import json
from typing import Any

from personal_tools.base import ToolResult
from personal_tools import memory_v2
from personal_tools.long_term_memory import Screened, owner_for_chat
from redaction import redact_text


def parse_remember(arg: str) -> tuple[str, str, str]:
    """Supports existing plain-text arg or {text, tier} for v2 calls."""
    value = (arg or "").strip()
    if value.startswith("{"):
        try:
            obj = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError("invalid memory.remember JSON")
        if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
            raise ValueError("memory.remember requires a text string")
        return obj["text"], obj.get("tier", "facts"), ("untrusted" if obj.get("source") == "untrusted" else "manual")
    return value, "facts", "manual"


def screen_write_arg(tool_name: str, arg: str) -> Screened:
    if tool_name == "memory.remember":
        try:
            text, tier, source = parse_remember(arg)
            from personal_tools.memory_v2 import _validate
            safe = _validate(text, tier, source)
            return Screened(json.dumps({"text": safe, "tier": tier, "source": source}, ensure_ascii=False))
        except ValueError as exc:
            return Screened("", str(exc))
    if tool_name == "memory.forget":
        from personal_tools.memory_v2 import _ID
        if not _ID.fullmatch((arg or "").strip()):
            return Screened("", "memory.forget requires a numeric #id")
        return Screened(arg.strip())
    return Screened(arg or "")


async def memory_remember(settings: Any, arg: str = "", *, operator_id: str = "",
                          channel: str = "", chat_id: str = "") -> ToolResult:
    if not memory_v2.enabled(settings):
        return ToolResult(False, "memory v2 is disabled")
    try:
        text, tier, source = parse_remember(arg)
        who = owner_for_chat(settings, operator_id, channel, chat_id)
        row = memory_v2.remember_fact(settings, who, text, tier=tier,
                                      source=source, source_channel=channel)
        return ToolResult(True, f"已记住 #{row['id']}（{row['tier']}）: {row['text']}")
    except ValueError as exc:
        return ToolResult(False, str(exc))


async def memory_forget(settings: Any, arg: str = "", *, operator_id: str = "",
                        channel: str = "", chat_id: str = "") -> ToolResult:
    try:
        who = owner_for_chat(settings, operator_id, channel, chat_id)
        rows, status = memory_v2.forget_fact(settings, who, arg)
        if status != "deleted":
            return ToolResult(False, "memory not found")
        return ToolResult(True, f"已删除 #{rows[0]['id']}")
    except ValueError as exc:
        return ToolResult(False, str(exc))


async def memory_list(settings: Any, arg: str = "", *, operator_id: str = "",
                      channel: str = "", chat_id: str = "") -> ToolResult:
    who = owner_for_chat(settings, operator_id, channel, chat_id)
    rows = memory_v2.list_facts(settings, who, limit=30)
    return ToolResult(True, "\n".join(
        f"#{r['id']} [{r['tier']}] {r['text']}" for r in rows
    ) or "还没有结构化记忆。")


async def memory_search(settings: Any, arg: str = "", *, operator_id: str = "",
                        channel: str = "", chat_id: str = "") -> ToolResult:
    if redact_text(arg) != arg:
        return ToolResult(False, "cannot search with a secret")
    who = owner_for_chat(settings, operator_id, channel, chat_id)
    rows = memory_v2.search_facts(settings, who, arg)
    return ToolResult(True, "\n".join(
        f"#{r['id']} [{r['tier']}, {r['source']}] {r['text']}" for r in rows
    ) or "没有找到匹配记忆。")
