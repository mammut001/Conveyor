"""handlers/dispatch.py — single entry point for any channel.

Both bot.py and feishu_bot.py call dispatch() with the same handler-side
inputs. Telegram-specific UI (inline buttons for /onboard) lives in the
Telegram adapter and is opted-in via port.supports_inline_buttons.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from channel.auth import is_allowed
from channel.types import InboundMessage, OutboundPort
from config import Settings
from handlers.chat import (
    chat_enabled,
    chat_or_agent,
    handle_chat_clear,
    handle_deep,
    is_time_sensitive,
    host_edit_imperative,
    readonly_host_tool,
    routes_to_agent,
    web_evidence,
)
from handlers.commands import parse_command, run_command
from handlers.context import detect_context_intent, handle_context_job, memo_text_with_quote
from handlers.intent import COMPUTER_STOP_TEXTS, RouteResult, desktop_followup_route, route_intent
from handlers.jobs import handle_codex_job
from handlers.memo import detect_memory_intent, handle_memo
from handlers.tools.runner import handle_hybrid, handle_route, try_resolve_confirmation
from refinement_store import should_route_refinement
from runner import CodexRunner, JobMode

logger = logging.getLogger(__name__)

# Snapshot tools that must not swallow an edit/run/debug imperative.
# "debug the disk usage bug" matches the disk-usage pattern and would
# otherwise return df without starting Codex.
_SNAPSHOT_TOOLS = frozenset({"disk", "service_status"})


def _edit_overrides_snapshot(text: str, route: RouteResult) -> bool:
    if route.kind != "deterministic":
        return False
    tools = frozenset(route.tools or ())
    if not tools or not tools <= _SNAPSHOT_TOOLS:
        return False
    return host_edit_imperative(text)


async def dispatch(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    runner: CodexRunner,
) -> None:
    if not msg.text.strip() and msg.reply_to is None and not msg.attachments:
        return

    if not is_allowed(msg, settings):
        await port.reply(msg, "Unauthorized.")
        return

    # Validate bindings before any fallback can silently run in the default
    # workspace. /agent remains available to explain an archived binding.
    from agents import AgentError, conversation_for_chat
    parsed = parse_command(msg.text)
    if parsed is not None and parsed[0] in ("agent", "workers"):
        await run_command(parsed[0], msg, port, runner, settings, parsed[1])
        return
    try:
        from handlers.workers import bind_execution
        msg, port = bind_execution(msg, port, settings)
    except AgentError as exc:
        await port.reply(msg, str(exc))
        return
    try:
        msg = replace(msg, chat_id=conversation_for_chat(
            settings, msg.channel, msg.chat_id,
            validate=not (parsed and parsed[0] in ("status", "last", "jobs", "cancel", "diff", "discard")),
        ))
    except AgentError as exc:
        await port.reply(msg, str(exc))
        return

    # Commands in a conversation act on that conversation's lane: /status,
    # /cancel, /diff and /apply in an agent's chat mean that agent's jobs.
    from job_lanes import runner_for_chat
    runner = runner_for_chat(runner, settings, msg.channel, msg.chat_id)

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
            await handle_deep(msg, port, runner, settings=settings)
            return
        if cmd_name == "chat_clear":
            await handle_chat_clear(msg, port, settings)
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

    text_clean = msg.text.strip().lower()
    if text_clean in COMPUTER_STOP_TEXTS:
        from handlers.tools.runner import run_tool
        text = await run_tool(settings, "computer.stop", "")
        await port.reply(msg, text)
        return

    if msg.attachments:
        await handle_context_job(msg, port, settings, runner)
        return

    if (
        getattr(settings, "long_term_memory_enabled", False)
        and getattr(settings, "chat_tools_enabled", False)
    ):
        from personal_tools.long_term_memory import (
            GROUP_REFUSAL, allowed_for, classify_explicit, screen_write_arg,
        )
        explicit = classify_explicit(msg.text)
        if explicit is not None:
            if not allowed_for(settings, msg):
                await port.reply(msg, GROUP_REFUSAL)
                return
            tool_name, raw_arg = explicit
            screened = screen_write_arg(tool_name, raw_arg)
            if screened.error:
                await port.reply(msg, screened.error)
                return
            from handlers.tools.runner import _request_confirmation
            await _request_confirmation(msg, port, settings, tool_name, screened.arg)
            return

    if detect_memory_intent(msg.text):
        if msg.reply_to is not None:
            await handle_memo(
                msg, port, runner,
                text=memo_text_with_quote(msg.text, msg.reply_to),
            )
            return
        await handle_memo(msg, port, runner)
        return

    route = route_intent(msg.text)
    if route.kind == "llm":
        followed = desktop_followup_route(settings, msg.text)
        if followed is not None:
            route = followed

    if msg.reply_to is not None and (
        detect_context_intent(msg.text) != "ask" or route.kind == "llm"
    ):
        await handle_context_job(msg, port, settings, runner)
        return

    if route.kind == "deterministic" and not _edit_overrides_snapshot(msg.text, route):
        await handle_route(msg, port, runner, settings, route)
        return
    if route.kind == "hybrid":
        await handle_hybrid(msg, port, runner, settings, route)
        return

    prompt = route.question or msg.text
    # Chat tools on: a read-only disk/service question that names the
    # operator machine is answered by the existing tool. That phrase does
    # not match the deterministic patterns above (no 看看/查/看), and it
    # must not fall through to a Codex job. Edit imperatives return no
    # tool and keep the Codex path below. Chat tools off: this is a no-op.
    host_tool = readonly_host_tool(msg.text, settings)
    if host_tool:
        await handle_route(
            msg, port, runner, settings,
            RouteResult(kind="deterministic", tools=(host_tool,)),
        )
        return
    # When the same stable session has an active (or still-pending first)
    # refinement chain, short edit feedback gets a conservative execution
    # route even if it contains no ordinary action keyword. Explanation and
    # question-shaped follow-ups are explicitly excluded by the helper. The
    # executed prompt remains the operator's own text — no model output is
    # ever converted into a new instruction.
    if chat_enabled(settings) and not routes_to_agent(msg.text, settings):
        if should_route_refinement(settings, msg):
            await handle_codex_job(
                msg, port, runner, mode=JobMode.FIX, prompt=prompt,
            )
            return
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
