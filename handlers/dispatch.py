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
from handlers.jobs import handle_codex_job
from handlers.memo import detect_memory_intent, handle_memo
from handlers.ops import detect_ops_intent, handle_ops_intent
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

    # Host-status fast path: phrases like "看看我的负载" or
    # "check vps load" route to /load /htop /ps without invoking
    # Codex. Conservative matching; coding requests about htop in
    # a sandbox are NOT hijacked.
    ops_kind = detect_ops_intent(msg.text)
    if ops_kind is not None:
        await handle_ops_intent(msg, port, runner, settings, ops_kind)
        return

    await handle_codex_job(msg, port, runner, mode=JobMode.RUN)
