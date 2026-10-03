"""handlers/chat_tools.py — tool bridge for the chat tier.

Exposes Conveyor's existing tools to chat models via OpenAI-compatible
function schemas, running READ tools automatically and requesting operator
confirmation for any WRITE / WRITE_SAFE tool.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from channel.types import InboundMessage, OutboundPort
from config import Settings
import handlers.tools.executors  # register builtin tools into TOOL_REGISTRY
from handlers.tools.registry import TOOL_REGISTRY, DangerLevel
from handlers.tools.runner import _request_confirmation, run_tool
from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
from redaction import redact_text
from runner.chat_client import ChatConfig, complete_chat

logger = logging.getLogger(__name__)

REVERSE_TOOL_MAP: dict[str, str] = {}


def tool_to_func_name(tool_name: str) -> str:
    """Map tool name to OpenAI function name matching ^[a-zA-Z0-9_-]{1,64}$."""
    return tool_name.replace(".", "__")


def func_to_tool_name(func_name: str) -> str:
    """Reverse map OpenAI function name to original tool name."""
    return REVERSE_TOOL_MAP.get(func_name, func_name.replace("__", "."))


def _get_tool_spec(name: str, settings: Settings | None = None) -> Any | None:
    if name == "agents.parallel":
        from handlers.subagents import get_subagent_tool_spec
        return get_subagent_tool_spec(settings)
    if name.startswith("mcp."):
        from mcp_client import get_mcp_tool_spec
        return get_mcp_tool_spec(settings, name)
    register_personal_tools()
    if name in PERSONAL_TOOL_REGISTRY:
        return PERSONAL_TOOL_REGISTRY[name]
    return TOOL_REGISTRY.get(name)


def format_tool_result(tool_name: str, raw_res: Any, max_len: int = 4000) -> str:
    """Format, redact, truncate, and wrap tool result as untrusted XML."""
    raw_str = redact_text(str(raw_res or ""))
    truncated = raw_str[:max_len] if len(raw_str) > max_len else raw_str
    neutralized = re.sub(r"</tool-result\s*>", "&lt;/tool-result&gt;", truncated, flags=re.IGNORECASE)
    return f'<tool-result name="{tool_name}" untrusted="true">\n{neutralized}\n</tool-result>'


# Read-only tools that reach arbitrary network targets. Tool output is fed back
# to the model, so a prompt-injected page could use these to exfiltrate data.
# Excluded from chat tool mode unless allowlisted via CONVEYOR_CHAT_TOOLS_NETWORK_ALLOW.
NETWORK_TOOLS = frozenset({
    "web.fetch", "web.text", "web.headers", "web.search",
    "research.run", "research.project",
})


def _network_allowlist(settings: Any) -> set[str]:
    raw = getattr(settings, "chat_tools_network_allow", ()) or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    items = {str(x).strip() for x in raw if str(x).strip()}
    if "*" in items or "all" in items:
        return set(NETWORK_TOOLS)
    return items & NETWORK_TOOLS


def is_exposed(name: str, spec: Any, settings: Any = None, *, memory_allowed: bool = True) -> bool:
    """Single policy used for both schema exposure and execution."""
    if spec is None or name.startswith("desktop."):
        return False
    if name == "agents.parallel":
        from config import is_subagents_enabled
        from handlers.subagents import is_in_subagent
        if not is_subagents_enabled(settings) or is_in_subagent():
            return False
        return True
    if name.startswith("mcp."):
        if not getattr(settings, "mcp_enabled", False):
            return False
        return spec.danger in (DangerLevel.READ, DangerLevel.WRITE_SAFE, DangerLevel.WRITE)
    if name.startswith("memory.") and not memory_allowed:
        return False
    if name.startswith("routine.") and not getattr(settings, "routines_enabled", False):
        return False
    if name.startswith("memory.") and not getattr(settings, "long_term_memory_enabled", False):
        return False
    if name.startswith("skill.") and not getattr(settings, "skills_enabled", False):
        return False
    if spec.danger not in (DangerLevel.READ, DangerLevel.WRITE_SAFE, DangerLevel.WRITE):
        return False
    if name in NETWORK_TOOLS and name not in _network_allowlist(settings):
        return False
    return True


def build_tool_schemas(settings: Settings | None = None, *, memory_allowed: bool = True) -> list[dict]:
    """OpenAI function schemas for exposed tools.

    Exposes personal tools and TOOL_REGISTRY tools whose danger is READ,
    WRITE_SAFE, or WRITE. Excludes DESTRUCTIVE tools, desktop.* tools, and
    network-reaching READ tools unless allowlisted.
    """
    register_personal_tools()
    schemas: list[dict] = []
    seen: set[str] = set()

    # Collect from personal tools first, then builtin tools
    candidates: list[tuple[str, Any]] = []
    for name, spec in PERSONAL_TOOL_REGISTRY.items():
        candidates.append((name, spec))
    for name, spec in TOOL_REGISTRY.items():
        candidates.append((name, spec))

    for name, spec in candidates:
        if name in seen:
            continue
        seen.add(name)

        if not is_exposed(name, spec, settings, memory_allowed=memory_allowed):
            continue

        func_name = tool_to_func_name(name)
        REVERSE_TOOL_MAP[func_name] = name

        danger_str = spec.danger.value if hasattr(spec.danger, "value") else str(spec.danger)
        desc = f"{spec.summary} [{danger_str}]"
        schema = {
            "type": "function",
            "function": {
                "name": func_name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "arg": {
                            "type": "string",
                            "description": "Free-text argument for the tool, same as the chat command argument; empty if none",
                        },
                    },
                    "required": [],
                },
            },
        }
        schemas.append(schema)

    if settings and getattr(settings, "mcp_enabled", False):
        from mcp_client import get_mcp_manager
        mcp_schemas = get_mcp_manager().get_exposed_schemas(settings)
        schemas.extend(mcp_schemas)

    from config import is_subagents_enabled
    if settings and is_subagents_enabled(settings):
        from handlers.subagents import (
            PARALLEL_TOOL_DESCRIPTION,
            PARALLEL_TOOL_NAME,
            PARALLEL_TOOL_PARAMETERS,
            get_subagent_tool_spec,
            is_in_subagent,
        )
        if not is_in_subagent():
            sub_spec = get_subagent_tool_spec(settings)
            if sub_spec and is_exposed(PARALLEL_TOOL_NAME, sub_spec, settings, memory_allowed=memory_allowed):
                func_name = tool_to_func_name(PARALLEL_TOOL_NAME)
                REVERSE_TOOL_MAP[func_name] = PARALLEL_TOOL_NAME
                danger_str = sub_spec.danger.value if hasattr(sub_spec.danger, "value") else str(sub_spec.danger)
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "description": f"{PARALLEL_TOOL_DESCRIPTION} [{danger_str}]",
                        "parameters": PARALLEL_TOOL_PARAMETERS,
                    },
                })

    return schemas


@dataclass
class ToolLoopResult:
    text: str
    tools_called: list[str] = field(default_factory=list)
    confirmation_requested: bool = False
    messages: list[dict] = field(default_factory=list)


def _parse_tool_arg(raw_args: Any) -> str:
    if isinstance(raw_args, dict):
        val = raw_args.get("arg", "")
        return str(val) if val is not None else ""
    if isinstance(raw_args, str):
        trimmed = raw_args.strip()
        if not trimmed:
            return ""
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict):
                val = parsed.get("arg", "")
                return str(val) if val is not None else ""
        except (json.JSONDecodeError, ValueError):
            return ""
    return ""


async def run_tool_loop(
    msg: InboundMessage,
    port: OutboundPort,
    settings: Settings,
    messages: list[dict],
    config: ChatConfig,
    *,
    placeholder: str | None = None,
) -> ToolLoopResult:
    """Execute up to chat_tool_max_steps rounds of complete_chat with tools."""
    from personal_tools.long_term_memory import allowed_for
    memory_allowed = allowed_for(settings, msg)
    if getattr(settings, "mcp_enabled", False):
        from mcp_client import get_mcp_manager
        try:
            await get_mcp_manager().ensure_fresh(settings)
        except Exception:
            logger.warning("MCP tool discovery failed", exc_info=True)
    schemas = build_tool_schemas(settings, memory_allowed=memory_allowed)
    max_steps_val = getattr(settings, "chat_tool_max_steps", 3)
    max_steps = 3 if max_steps_val is None else int(max_steps_val)
    max_steps = max(0, max_steps)
    tools_called: list[str] = []

    for _step in range(max_steps):
        resp_message = await complete_chat(config, messages, tools=schemas)
        tool_calls = resp_message.get("tool_calls")
        if not tool_calls:
            return ToolLoopResult(
                text=resp_message.get("content") or "",
                tools_called=tools_called,
                confirmation_requested=False,
                messages=messages,
            )

        messages.append(resp_message)

        for tc in tool_calls:
            tool_call_id = tc.get("id") or ""
            fn_info = tc.get("function") or {}
            func_name = fn_info.get("name") or ""
            real_tool_name = func_to_tool_name(func_name)

            if real_tool_name.startswith("mcp.") or real_tool_name == "agents.parallel":
                raw_arg_val = fn_info.get("arguments")
                arg_dict = None
                if isinstance(raw_arg_val, dict):
                    arg_dict = raw_arg_val
                elif isinstance(raw_arg_val, str):
                    trimmed = raw_arg_val.strip()
                    if trimmed:
                        try:
                            parsed = json.loads(trimmed)
                            if isinstance(parsed, dict):
                                arg_dict = parsed
                        except Exception:
                            arg_dict = None
                    else:
                        arg_dict = {}
                else:
                    arg_dict = None

                if arg_dict is None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "invalid arguments",
                    })
                    continue
                arg = json.dumps(arg_dict, sort_keys=True, ensure_ascii=False)
            else:
                arg = _parse_tool_arg(fn_info.get("arguments"))

            spec = _get_tool_spec(real_tool_name, settings)
            if not is_exposed(real_tool_name, spec, settings, memory_allowed=memory_allowed):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "unknown tool",
                })
                continue

            danger = spec.danger
            if real_tool_name.startswith("memory.") and danger in (
                DangerLevel.WRITE_SAFE, DangerLevel.WRITE,
            ):
                from personal_tools.long_term_memory import screen_write_arg
                screened = screen_write_arg(real_tool_name, arg)
                if screened.error:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": screened.error,
                    })
                    continue
                arg = screened.arg
            if danger in (DangerLevel.WRITE_SAFE, DangerLevel.WRITE):
                # Non-READ tools must NOT execute automatically
                logger.info("Chat tier tool call requested confirmation: %s", real_tool_name)
                tools_called.append(real_tool_name)
                await _request_confirmation(msg, port, settings, real_tool_name, arg)
                return ToolLoopResult(
                    text=resp_message.get("content") or "",
                    tools_called=tools_called,
                    confirmation_requested=True,
                    messages=messages,
                )

            if danger == DangerLevel.READ:
                logger.info("Chat tier tool call executed: %s", real_tool_name)
                tools_called.append(real_tool_name)
                if real_tool_name == "agents.parallel":
                    raw_res = await run_tool(
                        settings,
                        real_tool_name,
                        arg,
                        operator_id=msg.operator_id,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        port=port,
                        msg=msg,
                        config=config,
                        placeholder=placeholder,
                    )
                else:
                    raw_res = await run_tool(
                        settings,
                        real_tool_name,
                        arg,
                        operator_id=msg.operator_id,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                    )
                max_tasks = int(getattr(settings, "subagents_max_tasks", 4))
                max_output = int(getattr(settings, "subagents_max_output_chars", 2500))
                # Tool result cap: bounded at 16000 chars for agents.parallel specifically, else 4000
                tool_cap = min(16000, max_tasks * max_output) if real_tool_name == "agents.parallel" else 4000
                wrapped = format_tool_result(real_tool_name, raw_res, max_len=tool_cap)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": wrapped,
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "unknown tool",
                })

    # Step budget exhausted; make one last call without tools to get final answer
    final_msg = await complete_chat(config, messages, tools=None)
    return ToolLoopResult(
        text=final_msg.get("content") or "",
        tools_called=tools_called,
        confirmation_requested=False,
        messages=messages,
    )
