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


def _get_tool_spec(name: str) -> Any | None:
    register_personal_tools()
    if name in PERSONAL_TOOL_REGISTRY:
        return PERSONAL_TOOL_REGISTRY[name]
    return TOOL_REGISTRY.get(name)


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


def is_exposed(name: str, spec: Any, settings: Any = None) -> bool:
    """Single policy used for both schema exposure and execution."""
    if spec is None or name.startswith("desktop."):
        return False
    if spec.danger not in (DangerLevel.READ, DangerLevel.WRITE_SAFE, DangerLevel.WRITE):
        return False
    if name in NETWORK_TOOLS and name not in _network_allowlist(settings):
        return False
    return True


def build_tool_schemas(settings: Settings | None = None) -> list[dict]:
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

        if not is_exposed(name, spec, settings):
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
) -> ToolLoopResult:
    """Execute up to chat_tool_max_steps rounds of complete_chat with tools."""
    schemas = build_tool_schemas(settings)
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
            arg = _parse_tool_arg(fn_info.get("arguments"))

            spec = _get_tool_spec(real_tool_name)
            if not is_exposed(real_tool_name, spec, settings):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": "unknown tool",
                })
                continue

            danger = spec.danger
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
                raw_res = await run_tool(
                    settings,
                    real_tool_name,
                    arg,
                    operator_id=msg.operator_id,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                )
                # Tool output leaves the host (sent to the chat provider): redact secrets first.
                raw_str = redact_text(str(raw_res or ""))
                truncated = raw_str[:4000] if len(raw_str) > 4000 else raw_str
                neutralized = re.sub(r"</tool-result\s*>", "&lt;/tool-result&gt;", truncated, flags=re.IGNORECASE)
                wrapped = f'<tool-result name="{real_tool_name}" untrusted="true">\n{neutralized}\n</tool-result>'
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
