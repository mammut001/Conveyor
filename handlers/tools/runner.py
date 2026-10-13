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
from handlers.tools.audit import audit_tool_event
from handlers.tools.confirm import (
    create_pending,
    get_pending,
    is_cancellation_text,
    is_confirmation_text,
    matches_context,
    pop_pending,
    get_pending_for_context,
)
from handlers.tools.diagnose import build_hybrid_prompt, diagnose_tool_items, normalize_diagnose_mode
from handlers.tools.registry import get_tool, requires_confirmation
from runner import CodexRunner, JobMode

logger = logging.getLogger(__name__)

CALLBACK_CONFIRM_PREFIX = "tool:confirm:"
CALLBACK_CANCEL_PREFIX = "tool:cancel:"

_HYBRID_DEFAULT_TOOLS = ("load", "ps", "disk", "service_status")


def _requires_confirmation(tool_name: str, settings: Settings | None = None) -> bool:
    from handlers.tools.registry import DangerLevel
    if tool_name == "agents.parallel":
        return False
    # The desktop task tool has its own gate: blocked keywords refuse the
    # goal, and direct mode (always-direct or /computer_arm) must be on
    # before any click. A second confirmation would hide that reply and
    # stop a natural-language request from reaching the desktop.
    if tool_name == "computer.task":
        return False
    if tool_name == "github.create_issue" and getattr(settings, "draft_cards_enabled", False):
        return True
    if tool_name.startswith("mcp."):
        from mcp_client import get_mcp_tool_spec
        spec = get_mcp_tool_spec(settings, tool_name)
        if spec is not None:
            return spec.danger in (DangerLevel.WRITE, DangerLevel.DESTRUCTIVE)
        return True
    spec = get_tool(tool_name)
    if spec is not None:
        return requires_confirmation(spec)
    from personal_tools.registry import requires_personal_confirmation
    return requires_personal_confirmation(tool_name)


def _should_audit_no_confirm(tool_name: str) -> bool:
    """True for WRITE_SAFE tools: audit after execution but no confirmation."""
    from handlers.tools.registry import DangerLevel
    spec = get_tool(tool_name)
    if spec is not None:
        return spec.danger == DangerLevel.WRITE_SAFE
    from personal_tools.registry import get_personal_tool
    pspec = get_personal_tool(tool_name)
    return pspec is not None and pspec.danger == DangerLevel.WRITE_SAFE


async def run_tool(
    settings: Settings,
    tool_name: str,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
    port: Any = None,
    msg: Any = None,
    config: Any = None,
    placeholder: str | None = None,
    egress_approved: bool = False,
) -> str:
    import egress_taint
    if egress_taint.requires_approval(settings, tool_name, operator_id=operator_id, channel=channel, chat_id=chat_id) and not egress_approved:
        return "Outbound blocked: private data was read; approval required"
    egress_taint.mark_read(settings, tool_name, operator_id=operator_id, channel=channel, chat_id=chat_id)
    if tool_name == "agents.parallel":
        from handlers.subagents import execute_parallel_subagents
        return await execute_parallel_subagents(
            settings,
            arg,
            operator_id=operator_id,
            channel=channel,
            chat_id=chat_id,
            port=port,
            msg=msg,
            config=config,
            placeholder=placeholder,
        )
    if tool_name.startswith("mcp."):
        from mcp_client import run_mcp_tool
        return await run_mcp_tool(
            settings,
            tool_name,
            arg,
            operator_id=operator_id,
            channel=channel,
            chat_id=chat_id,
        )
    from personal_tools.registry import execute_personal_tool, get_personal_tool

    if get_personal_tool(tool_name) is not None:
        return await execute_personal_tool(
            settings,
            tool_name,
            arg,
            operator_id=operator_id,
            channel=channel,
            chat_id=chat_id,
        )
    spec = get_tool(tool_name)
    if spec is None:
        return f"未知工具: {tool_name}"
    try:
        if tool_name == "computer.task" and channel and chat_id:
            # The conversation decides which desktop the task runs on (an
            # agent's own, or the host's), so the task must know it.
            from handlers.tools.executors import exec_computer_task_for_chat
            return await exec_computer_task_for_chat(
                settings, arg, operator_id=operator_id, chat_id=chat_id, channel=channel,
            )
        return await spec.executor(settings, arg)
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return f"工具 {tool_name} 执行失败: {type(exc).__name__}"


async def run_tools(settings: Settings, tool_names: tuple[str, ...], arg: str = "",
                    *, operator_id: str = "", channel: str = "",
                    chat_id: str = "") -> str:
    parts: list[str] = []
    for name in tool_names:
        result = await run_tool(settings, name, arg,
                                operator_id=operator_id, channel=channel, chat_id=chat_id)
        parts.append(f"## tool:{name}\n{result}")
    return "\n\n".join(parts)


async def run_tools_collected(
    settings: Settings,
    items: tuple[tuple[str, str], ...],
    *, operator_id: str = "", channel: str = "", chat_id: str = "",
) -> str:
    parts: list[str] = []
    for name, arg in items:
        result = await run_tool(settings, name, arg,
                                operator_id=operator_id, channel=channel, chat_id=chat_id)
        parts.append(f"## tool:{name}\n{result}")
    return "\n\n".join(parts)


def _danger_label(tool_name: str, settings: Settings | None = None) -> str:
    if tool_name == "agents.parallel":
        return "read"
    if tool_name.startswith("mcp."):
        from mcp_client import get_mcp_tool_spec
        spec = get_mcp_tool_spec(settings, tool_name)
        if spec is not None:
            return spec.danger.value
        return "write"
    spec = get_tool(tool_name)
    if spec is not None:
        return spec.danger.value
    from personal_tools.registry import personal_tool_danger
    return personal_tool_danger(tool_name)


async def _tainted_network_guard(msg, port, settings, items):
    """Check multi-tool routes before any network-reaching READ is executed."""
    import egress_taint
    for name, arg in items:
        if egress_taint.requires_approval(
                settings, name, operator_id=msg.operator_id,
                channel=msg.channel, chat_id=msg.chat_id):
            await _request_confirmation(msg, port, settings, name, arg)
            return True
    return False


async def handle_route(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    route: RouteResult,
) -> None:
    """Execute deterministic tool(s) from a route result."""
    if route.tool_items:
        if await _tainted_network_guard(msg, port, settings, route.tool_items):
            return
        combined = await run_tools_collected(settings, route.tool_items,
            operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
        await port.reply(msg, combined)
        return
    if not route.tools:
        await port.reply(msg, "没有匹配的工具。")
        return
    if len(route.tools) == 1:
        await _invoke_tool(msg, port, settings, route.tools[0], route.arg, runner=runner)
        return
    if await _tainted_network_guard(msg, port, settings,
                                     ((name, route.arg) for name in route.tools)):
        return
    combined = await run_tools(settings, route.tools, route.arg,
        operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
    await port.reply(msg, combined)


async def handle_hybrid(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    route: RouteResult,
) -> None:
    """Collect deterministic facts, then ask Codex to analyze."""
    candidates = (route.tool_items if route.tool_items else
                  tuple((n, route.arg) for n in (
                      route.tools or _HYBRID_DEFAULT_TOOLS)))
    if await _tainted_network_guard(msg, port, settings, candidates):
        return
    if route.tool_items:
        facts = await run_tools_collected(settings, route.tool_items,
            operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
    elif route.tools:
        facts = await run_tools(settings, route.tools, route.arg,
            operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
    else:
        facts = await run_tools(settings, _HYBRID_DEFAULT_TOOLS, route.arg,
            operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
    question = route.question or msg.text.strip()
    prompt = build_hybrid_prompt(question, facts)
    await handle_codex_job(msg, port, runner, mode=JobMode.RUN, prompt=prompt)


async def handle_hybrid_project(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    tool_name: str,
    arg: str,
) -> None:
    """Run a project analysis tool that returns [HYBRID_PROMPT], then send to Codex."""
    if await _tainted_network_guard(msg, port, settings, ((tool_name, arg),)):
        return
    result = await run_tool(settings, tool_name, arg,
        operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
    if result.startswith("[HYBRID_PROMPT]"):
        prompt = result[len("[HYBRID_PROMPT]"):]
        await handle_codex_job(msg, port, runner, mode=JobMode.RUN, prompt=prompt)
    else:
        await port.reply(msg, result)


async def handle_diagnose_command(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    arg: str,
) -> None:
    mode = normalize_diagnose_mode(arg)
    if not mode:
        await port.reply(
            msg,
            "用法: /diagnose [server|bot|logs|quick]\n"
            "默认 server。示例: /diagnose server",
        )
        return
    tool_items = diagnose_tool_items(mode)
    question = f"/diagnose {mode} — 请分析 bot 主机状态并给出诊断建议"
    route = RouteResult(
        kind="hybrid",
        tool_items=tool_items,
        question=question,
    )
    await handle_hybrid(msg, port, runner, settings, route)


async def _invoke_tool(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    tool_name: str,
    arg: str,
    *,
    runner: CodexRunner | None = None,
) -> None:
    from personal_tools.registry import get_personal_tool

    spec = get_tool(tool_name)
    if spec is None and get_personal_tool(tool_name) is None and not tool_name.startswith("mcp."):
        await port.reply(msg, f"未知工具: {tool_name}")
        return
    if _memory_blocked(settings, msg, tool_name):
        from personal_tools.long_term_memory import GROUP_REFUSAL
        await port.reply(msg, GROUP_REFUSAL)
        return
    if tool_name in ("browser.read_webpage", "browser.browse_site"):
        if tool_name == "browser.browse_site":
            from personal_tools.browser_interact import approval_required
        else:
            from personal_tools.browser_read import approval_required
        try:
            needs_approval = approval_required(settings, arg, msg.operator_id)
        except Exception:
            await port.reply(msg, "browser denied: invalid_or_unavailable")
            return
        if needs_approval:
            await _request_confirmation(msg, port, settings, tool_name, arg)
            return
    import egress_taint
    if (_requires_confirmation(tool_name, settings) or
            egress_taint.requires_approval(settings, tool_name, operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)):
        await _request_confirmation(msg, port, settings, tool_name, arg)
        return
    if tool_name == "desktop.observe.request":
        from handlers.tools.observe_tools import exec_desktop_observe_request
        result = await exec_desktop_observe_request(
            settings, msg, arg or msg.text.strip(),
            port=port,
        )
    elif tool_name == "desktop.observe.status":
        from handlers.tools.observe_tools import exec_desktop_observe_status
        result = await exec_desktop_observe_status(settings, arg)
    elif tool_name == "desktop.observe.cancel":
        from handlers.tools.observe_tools import exec_desktop_observe_cancel
        result = await exec_desktop_observe_cancel(settings, arg)
    elif tool_name == "desktop.upload.request":
        from handlers.tools.observe_tools import exec_desktop_upload_request
        result = await exec_desktop_upload_request(settings, msg, arg)
    elif tool_name == "desktop.upload.status":
        from handlers.tools.observe_tools import exec_desktop_upload_status
        result = await exec_desktop_upload_status(settings, arg, port=port, msg=msg)
    elif tool_name == "desktop.upload.resend":
        from handlers.tools.observe_tools import exec_desktop_upload_resend
        result = await exec_desktop_upload_resend(settings, arg, port=port, msg=msg)
    elif tool_name == "desktop.upload.cancel":
        from handlers.tools.observe_tools import exec_desktop_upload_cancel
        result = await exec_desktop_upload_cancel(settings, arg)
    elif tool_name == "desktop.upload.cleanup":
        from handlers.tools.observe_tools import exec_desktop_upload_cleanup
        result = await exec_desktop_upload_cleanup(settings, arg)
    else:
        result = await run_tool(
            settings, tool_name, arg,
            operator_id=msg.operator_id,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
    if _should_audit_no_confirm(tool_name):
        audit_tool_event(
            settings,
            operator_id=msg.operator_id,
            chat_id=msg.chat_id,
            channel=msg.channel,
            tool_name=tool_name,
            arg=arg,
            danger=_danger_label(tool_name),
            action="executed",
            result_preview=result,
        )
    # If tool returns [HYBRID_PROMPT], send to Codex synthesis instead of replying raw.
    if result.startswith("[HYBRID_PROMPT]") and runner is not None:
        prompt = result[len("[HYBRID_PROMPT]"):]
        await handle_codex_job(msg, port, runner, mode=JobMode.RUN, prompt=prompt)
        return
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        try:
            if tool_name == "desktop.observe.request":
                from channel.feishu_cards import desktop_observe_request_card
                await port.send_card(msg, desktop_observe_request_card(result))
            elif tool_name in (
                "desktop.observe.status",
                "desktop.screenshot.status",
            ):
                from channel.feishu_cards import desktop_observe_status_card
                await port.send_card(msg, desktop_observe_status_card(result))
            elif tool_name == "desktop.upload.request":
                from channel.feishu_cards import desktop_upload_request_card
                await port.send_card(msg, desktop_upload_request_card(result))
            elif tool_name == "desktop.upload.status":
                from channel.feishu_cards import desktop_upload_status_card
                await port.send_card(msg, desktop_upload_status_card(result))
        except Exception:
            pass
    await port.reply(msg, result)


def _memory_blocked(settings: Settings, msg: InboundMessage, tool_name: str) -> bool:
    """memory.* is refused in group chats (shared memory must not leak there)."""
    if not tool_name.startswith("memory."):
        return False
    from personal_tools.long_term_memory import allowed_for
    return not allowed_for(settings, msg)


async def _request_confirmation(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    tool_name: str,
    arg: str,
) -> None:
    if _memory_blocked(settings, msg, tool_name):
        from personal_tools.long_term_memory import GROUP_REFUSAL
        await port.reply(msg, GROUP_REFUSAL)
        return
    if tool_name == "memory.forget" and getattr(settings, "memory_v2_enabled", False):
        from personal_tools.long_term_memory import owner_for_chat
        from personal_tools.memory_v2 import prepare_forget
        try:
            who = owner_for_chat(settings, msg.operator_id, msg.channel, msg.chat_id)
            arg = prepare_forget(settings, who, arg)
        except ValueError as exc:
            await port.reply(msg, str(exc))
            return
    spec = get_tool(tool_name)
    if spec is not None:
        summary = spec.summary
    elif tool_name.startswith("mcp."):
        from mcp_client import get_mcp_tool_spec
        mspec = get_mcp_tool_spec(settings, tool_name)
        summary = mspec.summary if mspec else tool_name
    else:
        from personal_tools.registry import get_personal_tool
        pspec = get_personal_tool(tool_name)
        summary = pspec.summary if pspec else tool_name
    if tool_name == "browser.browse_site":
        from personal_tools.browser_approval_scope import summary as browser_summary
        try:
            summary = browser_summary(arg)
        except Exception:
            await port.reply(msg, "browser denied: invalid_or_unavailable")
            return
    import egress_taint
    if (egress_taint.requires_approval(settings, tool_name, operator_id=msg.operator_id, channel=msg.channel, chat_id=msg.chat_id)
            and not (getattr(settings, "approval_inbox_enabled", False) or getattr(settings, "draft_cards_enabled", False))):
        await port.reply(msg, "Outbound blocked: private-data session requires Approval Inbox or Draft Cards")
        return
    import outbound_draft_cards as cards
    is_outbound_draft = cards.enabled(settings) and cards.is_outbound(settings, tool_name)
    if is_outbound_draft:
        try:
            cards._parse(settings, tool_name, arg)
        except ValueError as exc:
            await port.reply(msg, f"Outbound draft blocked: {exc}")
            return
    pending = create_pending(
        tool_name=tool_name,
        arg=arg,
        operator_id=msg.operator_id,
        chat_id=msg.chat_id,
        channel=msg.channel,
    )
    if is_outbound_draft:
        from handlers.tools.confirm import set_pending_ttl
        set_pending_ttl(pending.token, cards.expiry_seconds(settings))
        try:
            cards.register(settings, pending)
        except Exception:
            pop_pending(pending.token)
            await port.reply(msg, "Unable to save outbound draft; send blocked")
            return
    danger_label = ("external network access" if tool_name in ("browser.read_webpage", "browser.browse_site")
                    else _danger_label(tool_name, settings))
    source = "routine" if msg.chat_id.startswith("routine-") else "chat"
    try:
        import approval_relay
        approval_relay.publish(
            settings,
            pending,
            summary=summary,
            danger=danger_label,
            source=source,
        )
    except Exception:
        logger.debug("Failed to publish to approval relay", exc_info=True)
    audit_tool_event(
        settings,
        operator_id=msg.operator_id,
        chat_id=msg.chat_id,
        channel=msg.channel,
        tool_name=tool_name,
        arg=arg,
        danger=danger_label,
        action="requested",
    )
    text = (
        f"⚠️ 危险操作需确认\n\n"
        f"工具: {tool_name}\n"
        f"说明: {summary}\n"
        f"风险: {danger_label}\n"
    )
    if is_outbound_draft:
        from redaction import redact_text
        draft_preview = cards._parse(settings, tool_name, arg)
        for field, value in draft_preview.items():
            text += f"{field}: {redact_text(value)[:500]}\n"
        text += "Edit in Web Approval Inbox, or approve this exact original draft.\n"
    elif arg.strip():
        if tool_name == "service_restart":
            text += f"目标单元: {arg.strip()}\n"
        else:
            text += f"参数: {arg.strip()}\n"
    text += "\n确认执行？"
    if port.supports_inline_buttons:
        buttons = [[
            {"text": "✅ 确认", "callback_data": f"{CALLBACK_CONFIRM_PREFIX}{pending.token}"},
            {"text": "❌ 取消", "callback_data": f"{CALLBACK_CANCEL_PREFIX}{pending.token}"},
        ]]
        await port.reply_with_buttons(msg, text, buttons)
    elif msg.channel == "feishu" and hasattr(port, "send_card"):
        # Feishu confirmation: send a structured card with the
        # existing token. The callback handler feeds the token back
        # into the same confirmation binding, so the existing
        # operator + chat + channel + TTL checks still apply.
        try:
            from channel.feishu_cards import confirm_action_card
            card = confirm_action_card(
                token=pending.token,
                title="危险操作需确认",
                body=text,
                confirm_label="✅ 确认执行",
                cancel_label="❌ 取消",
            )
            await port.send_card(msg, card)
        except Exception:
            logger.debug("Feishu confirm_action_card send failed", exc_info=True)
            text += "\n\n回复「确认执行」执行，或「取消」放弃。"
            await port.reply(msg, text)
    else:
        text += "\n\n回复「确认执行」执行，或「取消」放弃。"
        await port.reply(msg, text)


def _audit_rejected(settings: Settings, action: PendingToolAction, msg: InboundMessage) -> None:
    audit_tool_event(
        settings,
        operator_id=msg.operator_id,
        chat_id=msg.chat_id,
        channel=msg.channel,
        tool_name=action.tool_name,
        arg=action.arg,
        danger=_danger_label(action.tool_name, settings),
        action="rejected",
        error_preview="context_mismatch",
    )


async def _relay_gate(
    settings: Settings,
    action: PendingToolAction,
    msg: InboundMessage,
    port: OutboundPort,
    approve: bool,
) -> bool:
    """First-decision-wins check against the shared approval relay.

    Returns True when the local resolution may proceed. When another surface
    already decided differently (or the shared row expired/finished), the
    local pending action is dropped and the operator is told why.
    """
    try:
        import approval_relay
        verdict = approval_relay.claim_local(settings, action.token, approve)
    except Exception:
        logger.exception("approval relay gate unavailable; blocking local action")
        verdict = "unavailable"
    if verdict == "ok":
        return True
    if verdict == "unavailable":
        # Keep the local action pending so a recovered relay can decide it.
        # In particular, never bypass a shared approval DB outage.
        await port.reply(msg, "审批状态暂时不可验证，操作已阻止。请稍后重试。")
        return False
    pop_pending(action.token)
    label = {
        "approved": "已批准",
        "rejected": "已拒绝",
        "expired": "已过期",
        "done": "已执行",
        "failed": "执行失败",
        "cancelled": "已取消",
    }.get(verdict, verdict)
    await port.reply(msg, f"该审批已在其他端处理（{label}），本次操作未执行。")
    return False


async def execute_confirmed(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    token: str,
) -> bool:
    """Run a previously confirmed dangerous tool. Returns True if handled."""
    action = get_pending(token)
    if action is None:
        await port.reply(msg, "确认已过期或无效，请重新发起。")
        return True
    if not matches_context(action, msg.operator_id, msg.chat_id, msg.channel):
        _audit_rejected(settings, action, msg)
        await port.reply(msg, "Unauthorized.")
        return True
    if action.tool_name.startswith("browser."):
        from browser_g3_kill import refusal
        reason = refusal(settings, action.tool_name)
        if reason:
            await port.reply(msg, reason)
            return True
    import outbound_draft_cards as cards
    if cards.is_draft_token(settings, token):
        outcome = await cards.approve_original(settings, token)
        pop_pending(token)
        await port.reply(msg, outcome['result'])
        return True
    if cards.enabled(settings) and cards.is_outbound(settings, action.tool_name):
        await port.reply(msg, 'Outbound draft unavailable; send blocked')
        return True
    if not await _relay_gate(settings, action, msg, port, approve=True):
        return True
    action = pop_pending(token)
    if action is None:
        await port.reply(msg, "确认已过期或无效，请重新发起。")
        return True
    audit_tool_event(
        settings,
        operator_id=action.operator_id,
        chat_id=action.chat_id,
        channel=action.channel,
        tool_name=action.tool_name,
        arg=action.arg,
        danger=_danger_label(action.tool_name, settings),
        action="confirmed",
    )
    try:
        if action.tool_name == "browser.g3_dispatch":
            from personal_tools.browser_submit import approve_and_submit
            result = await approve_and_submit(settings, action.arg,
                                              operator_id=action.operator_id)
        elif action.tool_name == "browser.browse_site":
            from personal_tools.browser_interact import approve_and_browse
            result = await approve_and_browse(settings, action.arg,
                                              operator_id=action.operator_id)
        elif action.tool_name == "browser.read_webpage":
            from personal_tools.browser_read import approve_and_read
            result = await approve_and_read(settings, action.arg,
                                            operator_id=action.operator_id)
        else:
            result = await run_tool(
                settings,
                action.tool_name,
                action.arg,
                operator_id=action.operator_id,
                channel=action.channel,
                chat_id=action.chat_id,
                egress_approved=True,
            )
    except Exception as exc:
        audit_tool_event(
            settings,
            operator_id=action.operator_id,
            chat_id=action.chat_id,
            channel=action.channel,
            tool_name=action.tool_name,
            arg=action.arg,
            danger=_danger_label(action.tool_name, settings),
            action="executed",
            error_preview=str(exc),
        )
        try:
            import approval_relay
            approval_relay.mark_local(settings, token, "failed", result_preview=str(exc))
        except Exception:
            pass
        await port.reply(msg, f"工具 {action.tool_name} 执行失败: {type(exc).__name__}")
        return True
    audit_tool_event(
        settings,
        operator_id=action.operator_id,
        chat_id=action.chat_id,
        channel=action.channel,
        tool_name=action.tool_name,
        arg=action.arg,
        danger=_danger_label(action.tool_name, settings),
        action="executed",
        result_preview=result,
    )
    try:
        import approval_relay
        approval_relay.mark_local(settings, token, "done", result_preview=result)
    except Exception:
        pass
    await port.reply(msg, result)
    return True


async def cancel_pending(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    token: str,
) -> bool:
    action = get_pending(token)
    if action is None:
        await port.reply(msg, "没有待确认的操作。")
        return True
    if not matches_context(action, msg.operator_id, msg.chat_id, msg.channel):
        _audit_rejected(settings, action, msg)
        await port.reply(msg, "Unauthorized.")
        return True
    import outbound_draft_cards as cards
    if cards.is_draft_token(settings, token):
        cards.deny(settings, token)
        pop_pending(token)
        try:
            import approval_relay
            approval_relay.mark_local(settings, token, 'cancelled')
        except Exception:
            pass
        await port.reply(msg, 'Outbound draft denied')
        return True
    if not await _relay_gate(settings, action, msg, port, approve=False):
        return True
    action = pop_pending(token)
    if action is None:
        await port.reply(msg, "没有待确认的操作。")
        return True
    audit_tool_event(
        settings,
        operator_id=action.operator_id,
        chat_id=action.chat_id,
        channel=action.channel,
        tool_name=action.tool_name,
        arg=action.arg,
        danger=_danger_label(action.tool_name),
        action="cancelled",
    )
    try:
        import approval_relay
        approval_relay.mark_local(settings, token, "cancelled")
    except Exception:
        pass
    label = action.arg.strip() or action.tool_name
    await port.reply(msg, f"已取消: {action.tool_name} ({label})")
    return True


async def try_resolve_confirmation(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
) -> bool:
    """Text-based YES/NO fallback (Feishu and Telegram). Returns True if consumed."""
    pending = get_pending_for_context(msg.operator_id, msg.chat_id, msg.channel)
    if pending is None:
        return False
    if is_confirmation_text(msg.text):
        return await execute_confirmed(msg, port, settings, pending.token)
    if is_cancellation_text(msg.text):
        return await cancel_pending(msg, port, settings, pending.token)
    return False


def parse_tool_callback(data: str) -> tuple[str, str] | None:
    """Return ('confirm'|'cancel', token) from callback_data."""
    if data.startswith(CALLBACK_CONFIRM_PREFIX):
        return "confirm", data[len(CALLBACK_CONFIRM_PREFIX):]
    if data.startswith(CALLBACK_CANCEL_PREFIX):
        return "cancel", data[len(CALLBACK_CANCEL_PREFIX):]
    return None
