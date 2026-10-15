"""Opt-in P3-2 extensions to the existing named AgentStore (no second registry).

Metadata is in the existing agents table; conversations, memory and routines
continue to use the existing per-agent identities. No schema/API changes when
CONVEYOR_NAMED_AGENTS is off.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from redaction import redact_text

_TOOL = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ID = re.compile(r"^[a-z0-9]{1,32}$")
_SCHEMA_READY = set()


def enabled(settings=None):
    on = os.environ.get("CONVEYOR_NAMED_AGENTS", "").lower() in ("1", "true", "yes", "on")
    return on and (settings is None or getattr(settings, "agents_enabled", False) is True)


def ensure_schema(db_path):
    db_path = Path(db_path).resolve()
    if db_path in _SCHEMA_READY and db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        # Serialize schema changes ACROSS PROCESSES. A deferred transaction
        # permits competing PRAGMA reads before the first ALTER.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, declaration in (
                ("persona", "TEXT NOT NULL DEFAULT ''"),
                ("memory_scope", "TEXT NOT NULL DEFAULT 'private'"),
                ("tools_allowlist", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                existing = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
                if name not in existing:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {name} {declaration}")
            conn.execute("UPDATE agents SET memory_scope='operator' "
                         "WHERE id='default' AND memory_scope != 'operator'")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _SCHEMA_READY.add(db_path)


def validate_extra(payload, *, agent_id=None):
    """Fail closed: never allow a named agent to borrow a different agent's memory."""
    out = {}
    if "persona" in payload:
        persona = payload["persona"]
        if not isinstance(persona, str) or len(persona) > 2000 or redact_text(persona) != persona:
            raise ValueError("persona must be a non-secret string of at most 2000 chars")
        out["persona"] = persona
    if "memory_scope" in payload:
        scope = payload["memory_scope"]
        permitted = "operator" if agent_id == "default" else "private"
        if scope != permitted:
            raise ValueError(f"memory_scope must be {permitted}")
        out["memory_scope"] = scope
    if "tools_allowlist" in payload:
        items = payload["tools_allowlist"]
        if not isinstance(items, list) or len(items) > 100 or any(
            not isinstance(v, str) or not _TOOL.fullmatch(v) for v in items
        ):
            raise ValueError("tools_allowlist must be an array of up to 100 exact tool names")
        out["tools_allowlist"] = json.dumps(sorted(set(items)), separators=(",", ":"))
    return out


def update_extra(db_path, agent_id, fields):
    if not fields:
        return
    if not _ID.fullmatch(agent_id):
        raise ValueError("invalid agent id")
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        with conn:
            cols = ", ".join(f"{name}=?" for name in fields)
            conn.execute(f"UPDATE agents SET {cols} WHERE id=?",
                         (*fields.values(), agent_id))
    finally:
        conn.close()


def public_agent(agent):
    if "tools_allowlist" in agent:
        try:
            agent["tools_allowlist"] = json.loads(agent["tools_allowlist"])
        except (ValueError, TypeError):
            agent["tools_allowlist"] = []  # malformed row must deny tools
    return agent


def active_agent(settings, channel, chat_id):
    import agents
    agent = agents.agent_for_chat(settings, channel, chat_id)
    if agent is None:
        # Unknown/archived named conversations must not inherit default privileges.
        if channel == "web" and str(chat_id).startswith(agents.AGENT_CHAT_PREFIX):
            return None
        return agents.AgentStore(settings).get("default")
    return agent


def tool_allowed(settings, channel, chat_id, name, *, operator_id="", context=None):
    # Context-free compatibility is preserved only while the P3-3 flag is OFF.
    import agent_context
    if agent_context.enabled(settings):
        return agent_context.allowed(settings, name, channel=channel,
                                     chat_id=chat_id, operator_id=operator_id,
                                     context=context)
    if not enabled(settings) or not channel or not chat_id:
        return True  # historical behavior with context enforcement off
    try:
        agent = active_agent(settings, channel, chat_id)
        if not agent or agent.get("archived"):
            return False
        if agent["id"] == "default":
            # Legacy/default conversations keep every pre-existing tool.
            # agent.message is new, and must not alter their schema or /tools.
            return name != "agent.message"
        allowlist = agent.get("tools_allowlist", [])
        if isinstance(allowlist, str):
            allowlist = json.loads(allowlist)
        return isinstance(allowlist, list) and name in allowlist
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def routine_belongs(settings, channel, chat_id, routine):
    if not enabled(settings):
        return True
    agent = active_agent(settings, channel, chat_id)
    if not agent or not routine:
        return False
    routine_id = routine.get("agent_id") or "default"
    return routine_id == agent["id"]


def message_spec():
    from types import SimpleNamespace
    from handlers.tools.registry import DangerLevel
    # Requires operator approval before writing to a different transcript.
    return SimpleNamespace(summary="Send an approved agent-to-agent transcript message (JSON: to, text)",
                           danger=DangerLevel.WRITE)


def send_message(settings, arg, *, channel, chat_id, operator_id=""):
    if not enabled(settings):
        return "unknown tool"
    # The legacy helper must not bypass mailbox/approval when Messaging V2 is on.
    import agent_messaging_v2
    if agent_messaging_v2.enabled(settings):
        return agent_messaging_v2.run(
            settings, "agent.message", arg, channel=channel, chat_id=chat_id,
            operator_id=operator_id, approved=False)
    sender = active_agent(settings, channel, chat_id)
    if not sender or not tool_allowed(settings, channel, chat_id, "agent.message",
                                      operator_id=operator_id):
        return "agent message denied"
    try:
        data = json.loads(arg)
        if not isinstance(data, dict) or set(data) != {"to", "text"}:
            raise ValueError()
        target_id, body = data["to"], data["text"]
        if not isinstance(target_id, str) or not _ID.fullmatch(target_id):
            raise ValueError()
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000:
            raise ValueError()
        target = __import__("agents").AgentStore(settings).get(target_id)
        if not target or target.get("archived") or target_id == sender["id"]:
            return "agent recipient denied"
        from transcript_store import get_transcript_store
        import agents
        transcript = get_transcript_store(settings)
        meta = {"from_agent": sender["id"], "to_agent": target_id, "untrusted": True}
        transcript.append(agents.session_id_for(target_id), "tool",
                          f"Message from agent {sender['name']}: {body}",
                          kind="agent_message", metadata=meta, channel="web",
                          operator_id="web-console", source_chat_id=agents.chat_id_for(target_id))
        transcript.append(agents.session_id_for(sender["id"]), "assistant",
                          f"To agent {target['name']}: {body}",
                          kind="agent_message", metadata=meta, channel="web",
                          operator_id="web-console", source_chat_id=agents.chat_id_for(sender["id"]))
        return "agent message recorded"
    except (TypeError, ValueError, KeyError):
        return "invalid agent message"
