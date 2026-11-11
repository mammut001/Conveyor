"""Human-approved agent delegation: durable, bounded task envelopes.

The body is never a prompt instruction or an approval; the worker revalidates
all requested capabilities against *both* current Agents before execution.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import agent_context as ac
import agent_mailbox as mailbox


NAME = "agent.delegate"
MAX_TASK_BYTES = 3500
MAX_TOOLS = 12
FORBIDDEN = frozenset({"agent.delegate", "agent.message", "agent.reply",
                       "agents.parallel", "subagent.spawn"})
_HUMAN_APPROVED: ContextVar[str | None] = ContextVar(
    "conveyor_delegation_human_approval", default=None)


@contextmanager
def approved_token(token: str):
    """Only execute_confirmed may install a validated, single-use approval."""
    marker = _HUMAN_APPROVED.set("delegate-" + token)
    try:
        yield
    finally:
        _HUMAN_APPROVED.reset(marker)


def enabled(settings) -> bool:
    return (mailbox.enabled(settings) and
            os.environ.get("CONVEYOR_AGENT_DELEGATION", "").lower()
            in ("1", "true", "yes", "on"))


def spec(settings):
    if not enabled(settings):
        return None
    from handlers.tools.registry import DangerLevel
    return SimpleNamespace(
        summary="Delegate one depth-1 bounded task for operator approval; JSON {to,task,tools_subset:[tool names]}. Never grants approvals.",
        danger=DangerLevel.WRITE)


def allowed_tools(settings, source, target, requested):
    """Intersect sender, target and global exposure; never trust body grants."""
    from handlers.chat_tools import _get_tool_spec, is_exposed
    from handlers.tools.registry import DangerLevel
    if (not isinstance(requested, list) or len(requested) > MAX_TOOLS or
            any(not isinstance(t, str) or not t or t in FORBIDDEN
                for t in requested)):
        raise mailbox.MailboxDenied("invalid delegated tool set")
    requested_set = set(requested)
    # WRITE tools may be proposed only to the existing human approval inbox;
    # they are never automatically executed by the child.
    available = set()
    for name in requested_set:
        current = _get_tool_spec(name, settings)
        if (source.permits(name) and target.permits(name) and current is not None
                and is_exposed(name, current, settings)
                and current.danger in (DangerLevel.READ, DangerLevel.WRITE, DangerLevel.WRITE_SAFE)):
            available.add(name)
    return frozenset(available)


def parse_request(arg):
    try:
        obj = json.loads(arg)
    except (TypeError, ValueError) as exc:
        raise mailbox.MailboxDenied("invalid delegate JSON") from exc
    if not isinstance(obj, dict) or set(obj) != {"to", "task", "tools_subset"}:
        raise mailbox.MailboxDenied("invalid delegate request")
    task = obj["task"]
    if (not isinstance(task, str) or not task.strip() or
            len(task.encode("utf-8")) > MAX_TASK_BYTES):
        raise mailbox.MailboxDenied("invalid task")
    return obj


def approval_arg(arg, token):
    """Retrying one approved action always reuses its deterministic key."""
    obj = parse_request(arg)
    obj["idempotency_key"] = f"delegate-{token}"
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def enqueue(settings, arg, *, channel, chat_id, operator_id, approved):
    if not enabled(settings):
        return "unknown tool"
    if not approved:
        return "human approval required"
    try:
        obj = json.loads(arg)
        if not isinstance(obj, dict) or set(obj) != {
                "to", "task", "tools_subset", "idempotency_key"}:
            raise mailbox.MailboxDenied("invalid approved delegation")
        task = obj["task"]
        if (not isinstance(task, str) or not task.strip() or
                len(task.encode("utf-8")) > MAX_TASK_BYTES):
            raise mailbox.MailboxDenied("invalid task")
        key = obj["idempotency_key"]
        if (not isinstance(key, str) or key != _HUMAN_APPROVED.get()):
            raise mailbox.MailboxDenied("verified human approval required")
        source = ac.for_request(settings, channel, chat_id, operator_id)
        if not source.permits(NAME):
            raise mailbox.MailboxDenied("delegation not allowed")
        target = mailbox._target(settings, source, obj["to"])
        granted = allowed_tools(settings, source, target, obj["tools_subset"])
        # The envelope is stored as untrusted data: never infer permission from
        # its tool list without intersecting again at worker execution.
        body = json.dumps({
            "v": 1, "task": task, "tools": sorted(granted),
        }, ensure_ascii=True, separators=(",", ":"))
        with ac.bind(source):
            row = mailbox.send(settings, sender=source, to_agent=target.agent_id,
                               kind="task", body=body, idempotency_key=key,
                               max_attempts=3)
        return f"agent task queued: {row['id']}"
    except (mailbox.MailboxDenied, mailbox.MailboxConflict,
            ac.AgentContextDenied, ValueError, TypeError, KeyError) as exc:
        import agent_security
        agent_security.audit(settings, operator=operator_id, event="deny",
            agent_id=agent_security.target_id(arg),
            reason=agent_security.denial_code(exc))
        return "agent delegation denied"


def resolve_child(settings, row):
    """Fresh, server-authoritative capability intersection for a lease."""
    import agents
    if row.get("kind") != "task" or row.get("depth") != 0:
        raise mailbox.MailboxDenied("invalid delegation depth/kind")
    if row.get("operator") != agents.WEB_OPERATOR:
        raise mailbox.MailboxDenied("cross-operator delegation")
    source = ac.resolve(settings, "web", agents.chat_id_for(row["from_agent"]),
                        agents.WEB_OPERATOR)
    target = ac.resolve(settings, "web", agents.chat_id_for(row["to_agent"]),
                        agents.WEB_OPERATOR)
    envelope = json.loads(row["body"])
    if (not isinstance(envelope, dict) or set(envelope) != {"v", "task", "tools"}
            or envelope["v"] != 1 or not isinstance(envelope["task"], str)
            or len(envelope["task"].encode("utf-8")) > MAX_TASK_BYTES):
        raise mailbox.MailboxDenied("invalid task envelope")
    selected = allowed_tools(settings, source, target, envelope["tools"])
    # No recursion: delegate is excluded regardless of source's grants.
    delegated = ac.delegate(settings, source, target, selected, row["id"])
    return delegated, envelope["task"], selected
