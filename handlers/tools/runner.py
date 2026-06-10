"""handlers/tools/runner.py — execute tools with safety policy and hybrid path.

- Safe (READ) tools run immediately.
- WRITE/DESTRUCTIVE tools require explicit confirmation.
- Hybrid path collects facts from multiple tools then passes them to Codex.
"""
from __future__ import annotations

import logging

from channel.types import InboundMessage, OutboundPort
from config import Settings
from handlers.intent import RouteResult
from handlers.jobs import handle_codex_job
from handlers.tools.confirm import (
    create_pending,
    get_pending,
    is_cancellation_text,
    is_confirmation_text,
    pop_pending,
    get_pending_for_operator,
)
from handlers.tools.registry import get_tool, requires_confirmation
from runner import CodexRunner, JobMode

logger = logging.getLogger(__name__)

CALLBACK_CONFIRM_PREFIX = "tool:confirm:"
CALLBACK_CANCEL_PREFIX = "tool:cancel:"


async def run_tool(settings: Settings, tool_name: str, arg: str = "") -> str:
    spec = get_tool(tool_name)
    if spec is None:
        return f"未知工具: {tool_name}"
    try:
        return await spec.executor(settings, arg)
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return f"工具 {tool_name} 执行失败: {type(exc).__name__}"


async def run_tools(settings: Settings, tool_names: tuple[str, ...], arg: str = "") -> str:
    parts: list[str] = []
    for name in tool_names:
        result = await run_tool(settings, name, arg)
        parts.append(f"## tool:{name}\n{result}")
    return "\n\n".join(parts)


async def handle_route(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    route: RouteResult,
) -> None:
    """Execute deterministic tool(s) from a route result."""
    if not route.tools:
        await port.reply(msg, "没有匹配的工具。")
        return
    if len(route.tools) == 1:
        await _invoke_tool(msg, port, settings, route.tools[0], route.arg)
        return
    combined = await run_tools(settings, route.tools, route.arg)
    await port.reply(msg, combined)


async def handle_hybrid(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    route: RouteResult,
) -> None:
    """Collect deterministic facts, then ask Codex to analyze."""
    tools = route.tools or ("load", "ps", "disk", "service_status")
    facts = await run_tools(settings, tools, route.arg)
    question = route.question or msg.text.strip()
    prompt = (
        f"用户问题：{question}\n\n"
        "以下是 bot 主机上的确定性采集数据（本地快照，非 Codex sandbox 猜测）：\n\n"
        f"{facts}\n\n"
        "请基于以上真实数据回答用户问题。如果数据不足以回答，说明还需要什么信息。"
        "不要编造主机状态；只分析已提供的数据。"
    )
    await handle_codex_job(msg, port, runner, mode=JobMode.RUN, prompt=prompt)


async def _invoke_tool(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    tool_name: str,
    arg: str,
) -> None:
    spec = get_tool(tool_name)
    if spec is None:
        await port.reply(msg, f"未知工具: {tool_name}")
        return
    if requires_confirmation(spec):
        await _request_confirmation(msg, port, tool_name, arg)
        return
    result = await run_tool(settings, tool_name, arg)
    await port.reply(msg, result)


async def _request_confirmation(
    msg: InboundMessage,
    port: OutboundPort,
    tool_name: str,
    arg: str,
) -> None:
    spec = get_tool(tool_name)
    summary = spec.summary if spec else tool_name
    pending = create_pending(
        tool_name=tool_name,
        arg=arg,
        operator_id=msg.operator_id,
        chat_id=msg.chat_id,
        channel=msg.channel,
    )
    text = (
        f"⚠️ 危险操作需确认\n\n"
        f"工具: {tool_name}\n"
        f"说明: {summary}\n"
    )
    if arg.strip():
        text += f"参数: {arg.strip()}\n"
    text += "\n确认执行？"
    if port.supports_inline_buttons:
        buttons = [[
            {"text": "✅ 确认", "callback_data": f"{CALLBACK_CONFIRM_PREFIX}{pending.token}"},
            {"text": "❌ 取消", "callback_data": f"{CALLBACK_CANCEL_PREFIX}{pending.token}"},
        ]]
        await port.reply_with_buttons(msg, text, buttons)
    else:
        text += "\n\n回复「确认」执行，或「取消」放弃。"
        await port.reply(msg, text)


async def execute_confirmed(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    token: str,
) -> bool:
    """Run a previously confirmed dangerous tool. Returns True if handled."""
    action = pop_pending(token)
    if action is None:
        await port.reply(msg, "确认已过期或无效，请重新发起。")
        return True
    if action.operator_id != msg.operator_id:
        await port.reply(msg, "Unauthorized.")
        return True
    result = await run_tool(settings, action.tool_name, action.arg)
    await port.reply(msg, result)
    return True


async def cancel_pending(
    msg: InboundMessage,
    port: OutboundPort,
    token: str,
) -> bool:
    action = pop_pending(token)
    if action is None:
        await port.reply(msg, "没有待确认的操作。")
        return True
    if action.operator_id != msg.operator_id:
        await port.reply(msg, "Unauthorized.")
        return True
    await port.reply(msg, f"已取消: {action.tool_name}")
    return True


async def try_resolve_confirmation(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
) -> bool:
    """Text-based YES/NO fallback (Feishu and Telegram). Returns True if consumed."""
    if is_confirmation_text(msg.text):
        pending = get_pending_for_operator(msg.operator_id)
        if pending is not None:
            return await execute_confirmed(msg, port, settings, pending.token)
    if is_cancellation_text(msg.text):
        pending = get_pending_for_operator(msg.operator_id)
        if pending is not None:
            return await cancel_pending(msg, port, pending.token)
    return False


def parse_tool_callback(data: str) -> tuple[str, str] | None:
    """Return ('confirm'|'cancel', token) from callback_data."""
    if data.startswith(CALLBACK_CONFIRM_PREFIX):
        return "confirm", data[len(CALLBACK_CONFIRM_PREFIX):]
    if data.startswith(CALLBACK_CANCEL_PREFIX):
        return "cancel", data[len(CALLBACK_CANCEL_PREFIX):]
    return None
