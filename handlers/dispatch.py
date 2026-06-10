"""handlers/dispatch.py — single entry point for any channel.

Both bot.py and feishu_bot.py call dispatch() with the same handler-side
inputs. Telegram-specific UI (inline buttons for /onboard) lives in the
Telegram adapter and is opted-in via port.supports_inline_buttons.
"""
from __future__ import annotations

import logging

from channel.auth import is_allowed
from channel.types import InboundMessage, OutboundPort
from config import Settings
from handlers.commands import parse_command, run_command
from handlers.intent import route_intent
from handlers.jobs import handle_codex_job
from handlers.memo import detect_memory_intent, handle_memo
from handlers.tools.runner import handle_hybrid, handle_route, try_resolve_confirmation
from runner import CodexRunner, JobMode

logger = logging.getLogger(__name__)


async def dispatch(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    runner: CodexRunner,
) -> None:
    if not msg.text.strip():
        return

    if not is_allowed(msg, settings):
        await port.reply(msg, "Unauthorized.")
        return

    # Dangerous-tool text confirmation (YES/取消) before other routing.
    if await try_resolve_confirmation(msg, port, settings):
        return

    parsed = parse_command(msg.text)
    if parsed is not None:
        cmd_name, arg = parsed
        if cmd_name == "memo":
            if not arg:
                await port.reply(msg, "用法：/memo <内容>")
                return
            await handle_memo(msg, port, runner, text=f"记 {arg}")
            return
        if cmd_name in ("run", "fix"):
            if not arg:
                await port.reply(msg, f"用法：/{cmd_name} <prompt>")
                return
            mode = JobMode.FIX if cmd_name == "fix" else JobMode.RUN
            await handle_codex_job(msg, port, runner, mode=mode, prompt=arg)
            return
        handled = await run_command(cmd_name, msg, port, runner, settings, arg)
        if handled:
            return
        await port.reply(msg, f"未知命令 /{cmd_name}。发送 /help 查看。")
        return

    if detect_memory_intent(msg.text):
        await handle_memo(msg, port, runner)
        return

    # Agent tool layer: deterministic tools, hybrid (tools + Codex), or LLM.
    route = route_intent(msg.text)
    if route.kind == "deterministic":
        await handle_route(msg, port, runner, settings, route)
        return
    if route.kind == "hybrid":
        await handle_hybrid(msg, port, runner, settings, route)
        return

    await handle_codex_job(msg, port, runner, mode=JobMode.RUN)
