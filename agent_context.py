"""Canonical, server-derived agent execution identity (P3-3 foundation).

This is an opt-in authorization boundary, not a second AgentStore.
Callers cannot nominate a memory owner or increase capability grants.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import json
import os
import re

_ID = re.compile(r"^[a-z0-9]{1,32}$")
_CHANNELS = frozenset({"web", "telegram", "feishu"})
_ACTIVE: ContextVar[AgentContext | None] = ContextVar("conveyor_agent_context", default=None)


class AgentContextDenied(ValueError):
    """Safe fail-closed identity or permission failure."""


@dataclass(frozen=True)
class AgentContext:
    agent_id: str
    session_id: str
    memory_owner: str
    operator_identity: str
    origin_channel: str
    tool_capabilities: frozenset[str]
    delegation_id: str | None = None
    trusted_owner_request: bool = False

    def permits(self, tool: str) -> bool:
        return tool in self.tool_capabilities or (
            "*" in self.tool_capabilities and tool != "agent.message"
        )


def enabled(settings) -> bool:
    import named_agents
    return (os.environ.get("CONVEYOR_AGENT_CONTEXT", "").lower() in
            ("1", "true", "yes", "on") and named_agents.enabled(settings))


def resolve(settings, channel: str, chat_id: str, operator_id: str,
            *, trusted_owner_request: bool = False) -> AgentContext:
    """Resolve identity exclusively from a verified channel/chat/operator tuple."""
    import agents
    from transcript_store import session_identity

    if (channel not in _CHANNELS or not isinstance(chat_id, str) or not chat_id
            or not isinstance(operator_id, str) or not operator_id):
        raise AgentContextDenied("agent identity required")
    try:
        agent = agents.agent_for_chat(settings, channel, chat_id)
        if not agent or agent.get("archived") or not _ID.fullmatch(str(agent["id"])):
            raise AgentContextDenied("unknown or archived agent")
        agent_id = agent["id"]
        if channel == "web" and chat_id.startswith(agents.AGENT_CHAT_PREFIX):
            if chat_id != agents.chat_id_for(agent_id) or operator_id != agents.WEB_OPERATOR:
                raise AgentContextDenied("invalid agent conversation")
        if agent_id == agents.DEFAULT_AGENT_ID:
            caps = frozenset({"*"})
            owner = operator_id
        else:
            if agent.get("memory_scope") != "private":
                raise AgentContextDenied("invalid agent memory scope")
            raw = agent.get("tools_allowlist", [])
            caps = json.loads(raw) if isinstance(raw, str) else raw
            if (not isinstance(caps, list) or
                any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", item)
                    for item in caps)):
                raise AgentContextDenied("invalid agent tool policy")
            caps = frozenset(caps)
            owner = f"agent:{agent_id}"
        return AgentContext(
            agent_id=agent_id,
            session_id=session_identity(channel, chat_id, operator_id),
            memory_owner=owner,
            operator_identity=operator_id,
            origin_channel=channel,
            tool_capabilities=caps,
            trusted_owner_request=bool(trusted_owner_request),
        )
    except AgentContextDenied:
        raise
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise AgentContextDenied("agent identity unavailable") from exc


def validated(settings, ctx: AgentContext, channel: str, chat_id: str,
              operator_id: str) -> AgentContext:
    """Re-resolve on each security boundary: revocation never trusts a snapshot."""
    if not isinstance(ctx, AgentContext):
        raise AgentContextDenied("agent context required")
    actual = resolve(settings, channel, chat_id, operator_id)
    capability_ok = (
        (ctx.tool_capabilities == frozenset({"*"}) or
         ("*" not in ctx.tool_capabilities and
          "agent.message" not in ctx.tool_capabilities))
        if "*" in actual.tool_capabilities
        else ctx.tool_capabilities.issubset(actual.tool_capabilities)
    )
    if (ctx.agent_id != actual.agent_id or ctx.session_id != actual.session_id
            or ctx.memory_owner != actual.memory_owner
            or ctx.operator_identity != actual.operator_identity
            or ctx.origin_channel != actual.origin_channel or not capability_ok):
        raise AgentContextDenied("agent context mismatch")
    # "*" cannot be conferred by nondefault agents through delegation.
    if "*" in ctx.tool_capabilities and "*" not in actual.tool_capabilities:
        raise AgentContextDenied("invalid delegated capability")
    return ctx


def for_request(settings, channel: str, chat_id: str, operator_id: str) -> AgentContext:
    """Use a bound identity if present; otherwise require explicit call identity."""
    bound = _ACTIVE.get()
    if bound is not None:
        return validated(settings, bound, channel, chat_id, operator_id)
    return resolve(settings, channel, chat_id, operator_id)


def allowed(settings, name: str, *, channel: str, chat_id: str,
            operator_id: str, context: AgentContext | None = None) -> bool:
    if not enabled(settings):
        return True
    try:
        ctx = (validated(settings, context, channel, chat_id, operator_id)
               if context is not None else for_request(settings, channel, chat_id, operator_id))
        return ctx.permits(name)
    except AgentContextDenied:
        return False


def _chat_id(ctx: AgentContext) -> str:
    prefix = f"{ctx.origin_channel}:{ctx.operator_identity}:"
    if not ctx.session_id.startswith(prefix):
        raise AgentContextDenied("invalid agent session")
    return ctx.session_id[len(prefix):]


def delegate(settings, source: AgentContext, target: AgentContext,
             requested: set[str] | frozenset[str], delegation_id: str) -> AgentContext:
    """Server-side capability intersection; never inherit a target's extra tools."""
    source = validated(settings, source, source.origin_channel, _chat_id(source),
                       source.operator_identity)
    target = validated(settings, target, target.origin_channel, _chat_id(target),
                       target.operator_identity)
    if not delegation_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", delegation_id):
        raise AgentContextDenied("invalid delegation")
    if not isinstance(source, AgentContext) or not isinstance(target, AgentContext):
        raise AgentContextDenied("agent context required")
    if not isinstance(requested, (set, frozenset)) or not all(isinstance(x, str) for x in requested):
        raise AgentContextDenied("invalid requested capabilities")
    # A '*' represents the built-in default agent's existing tool inventory.
    permitted = frozenset(tool for tool in requested
                          if source.permits(tool) and target.permits(tool))
    return replace(target, tool_capabilities=permitted, delegation_id=delegation_id,
                   trusted_owner_request=False)


@contextmanager
def bind(ctx: AgentContext):
    if not isinstance(ctx, AgentContext):
        raise AgentContextDenied("agent context required")
    token = _ACTIVE.set(ctx)
    try:
        yield ctx
    finally:
        _ACTIVE.reset(token)


def current() -> AgentContext | None:
    return _ACTIVE.get()


def history_key(settings, channel: str, chat_id: str, operator_id: str) -> str:
    ctx = for_request(settings, channel, chat_id, operator_id)
    return f"agent:{ctx.agent_id}:{ctx.session_id}"


def scoped_inbound(func):
    """Bind a canonical context across awaits and spawned tasks of inbound handlers."""
    @wraps(func)
    async def wrapped(msg, port, settings, *args, **kwargs):
        if not enabled(settings):
            return await func(msg, port, settings, *args, **kwargs)
        try:
            # Channel authentication is enforced at the adapter/dispatcher.
            # Unconfigured Feishu bootstrap mode is not a trusted owner.
            from channel.auth import is_allowed
            trusted = bool(is_allowed(msg, settings))
            if msg.channel == "feishu" and not getattr(settings, "lark_allowed_open_id", None):
                trusted = False
            ctx = resolve(settings, msg.channel, msg.chat_id, msg.operator_id,
                          trusted_owner_request=trusted)
        except AgentContextDenied:
            await port.reply(msg, "agent identity denied")
            return None
        with bind(ctx):
            return await func(msg, port, settings, *args, **kwargs)
    return wrapped
