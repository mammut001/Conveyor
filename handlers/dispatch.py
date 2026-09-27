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
from handlers.chat import (
    chat_enabled,
    chat_or_agent,
    handle_deep,
    is_time_sensitive,
    needs_agent,
    web_evidence,
)
from handlers.commands import parse_command, run_command
from handlers.context import (
    detect_context_intent,
    handle_context_job,
    memo_text_with_quote,
)
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
    # A bare "@bot" reply to a message, or a photo without a caption, is a
    # real request ("explain this"), so an empty text only ends here when
    # there is nothing quoted or attached either.
    if not msg.text.strip() and msg.reply_to is None and not msg.attachments:
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
        if cmd_name == "deep":
            await handle_deep(msg, port, runner)
            return
        if cmd_name in ("run", "fix"):
            if not arg:
                await port.reply(msg, f"用法：/{cmd_name} <prompt>")
                return
            mode = JobMode.FIX if cmd_name == "fix" else JobMode.RUN
            if msg.reply_to is not None or msg.attachments:
                await handle_context_job(
                    msg, port, settings, runner, question=arg, mode=mode,
                )
                return
            await handle_codex_job(msg, port, runner, mode=mode, prompt=arg)
            return
        handled = await run_command(cmd_name, msg, port, runner, settings, arg)
        if handled:
            return
        await port.reply(msg, f"未知命令 /{cmd_name}。发送 /help 查看。")
        return

    # Telegram stop fast path
    text_clean = msg.text.strip().lower()
    if text_clean in ("停下", "别动", "停止操作", "stop computer", "cancel computer task"):
        from handlers.tools.runner import run_tool
        text = await run_tool(settings, "computer.stop", "")
        await port.reply(msg, text)
        return

    # Images (sent, or on the replied-to message) always go to the agent:
    # no deterministic tool can look at a picture.
    if msg.attachments:
        await handle_context_job(msg, port, settings, runner)
        return

    if detect_memory_intent(msg.text):
        if msg.reply_to is not None:
            # "记一下" replying to a message saves the quoted message.
            await handle_memo(
                msg, port, runner,
                text=memo_text_with_quote(msg.text, msg.reply_to),
            )
            return
        await handle_memo(msg, port, runner)
        return

    # Agent tool layer: deterministic tools, hybrid (tools + Codex), or LLM.
    # Routing only ever looks at the operator's own words.
    route = route_intent(msg.text)

    # Reply / quote context ("@bot is this true?" on someone's message).
    # A request about the quoted message (fact-check, explain, summarize,
    # translate, or a free-form question) answers with it as context; a
    # plain tool request that merely happens to be a reply ("服务器状态"
    # replying to an old bot message) keeps its fast tool route.
    if msg.reply_to is not None and (
        detect_context_intent(msg.text) != "ask" or route.kind == "llm"
    ):
        await handle_context_job(msg, port, settings, runner)
        return

    if route.kind == "deterministic":
        await handle_route(msg, port, runner, settings, route)
        return
    if route.kind == "hybrid":
        await handle_hybrid(msg, port, runner, settings, route)
        return

    prompt = route.question or msg.text
    # Intent mode: conversation goes to the chat tier; clear execution
    # requests (and everything when the chat tier is off) go to Codex.
    if chat_enabled(settings) and not needs_agent(msg.text):
        evidence = await web_evidence(settings, msg.text) if is_time_sensitive(msg.text) else ""
        await chat_or_agent(
            msg, port, settings, runner,
            question=msg.text, codex_prompt=prompt, evidence=evidence,
        )
        return
    await handle_codex_job(
        msg,
        port,
        runner,
        mode=JobMode.RUN,
        prompt=prompt,
    )
