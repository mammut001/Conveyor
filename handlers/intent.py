"""handlers/intent.py — lightweight intent router for the agent tool layer.

Routes free-text messages to one of three paths:
- deterministic: run registered tools directly (no Codex)
- hybrid: collect tool facts, then pass to Codex for analysis
- llm: open-ended coding/debugging → Codex only

Conservative matching: false negatives preferred over hijacking
coding requests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from handlers.ops import detect_ops_intent
from handlers.tools.registry import TOOL_REGISTRY

# Ensure builtin tools are registered before route_intent uses TOOL_REGISTRY.
import handlers.tools.executors  # noqa: F401

RouteKind = Literal["deterministic", "hybrid", "llm"]


@dataclass(frozen=True)
class RouteResult:
    kind: RouteKind
    tools: tuple[str, ...] = ()
    question: str = ""
    arg: str = ""


# ---- Hybrid: diagnosis / analysis questions --------------------------------

_HYBRID_PATTERNS = (
    re.compile(
        r"(为什么|为啥|怎么回事|分析一下|诊断|help.*分析|why\s+is|what.*wrong)"
        r".*(慢|卡|高|异常|problem|slow|high|issue|down)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(服务器|vps|主机|机器|server|host).*(慢|卡|异常|问题|issue|slow)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(帮|help).*(看看|分析|诊断|check).*(服务器|vps|负载|server|load)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(为什么|why).*(负载|load|cpu|内存|memory|disk).*(高|满|full|high)",
        re.IGNORECASE,
    ),
)

_HYBRID_DEFAULT_TOOLS = ("load", "ps", "disk", "service_status")


# ---- Deterministic tool patterns (beyond legacy ops) -----------------------

_DISK_PATTERNS = (
    re.compile(r"(看看|查|看).*(磁盘|disk|df|空间|storage)", re.IGNORECASE),
    re.compile(r"(disk|storage)\s*(usage|space|full)", re.IGNORECASE),
)
_LOGS_PATTERNS = (
    re.compile(r"(看看|查|看|tail).*(日志|log|journal)", re.IGNORECASE),
    re.compile(r"(journalctl|service\s+log)", re.IGNORECASE),
)
_SERVICE_PATTERNS = (
    re.compile(r"(服务|service|bot).*(状态|status|还在|running|alive)", re.IGNORECASE),
    re.compile(r"(systemctl|服务状态)", re.IGNORECASE),
)
_GIT_PATTERNS = (
    re.compile(r"\bgit\s+status\b", re.IGNORECASE),
    re.compile(r"(代码|git).*(改了什么|变更|改动|diff|status)", re.IGNORECASE),
)
_RESTART_PATTERNS = (
    re.compile(r"(重启|restart).*(bot|服务|service|conveyor|telegram|feishu)", re.IGNORECASE),
)


def route_intent(text: str) -> RouteResult:
    """Classify user text into deterministic / hybrid / llm path."""
    body = (text or "").strip()
    if not body:
        return RouteResult(kind="llm")

    # Explicit ops/tool requests win over hybrid diagnosis patterns.
    # "帮我运行 htop 看看我的 vps" is a snapshot request, not analysis.
    ops_kind = detect_ops_intent(body)
    if ops_kind == "htop":
        return RouteResult(kind="deterministic", tools=("htop",))
    if ops_kind == "ps":
        return RouteResult(kind="deterministic", tools=("ps",))
    if ops_kind == "load":
        return RouteResult(kind="deterministic", tools=("load",))

    for pat in _RESTART_PATTERNS:
        if pat.search(body):
            arg = _extract_service_arg(body)
            return RouteResult(kind="deterministic", tools=("service_restart",), arg=arg)
    for pat in _DISK_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("disk",))
    for pat in _LOGS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("logs",), arg=_extract_log_arg(body))
    for pat in _SERVICE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("service_status",))
    for pat in _GIT_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("git_status",))

    tool_match = _match_explicit_tool(body)
    if tool_match is not None:
        return RouteResult(kind="deterministic", tools=(tool_match,))

    # Hybrid: diagnosis / analysis questions (tools first, then Codex).
    for pat in _HYBRID_PATTERNS:
        if pat.search(body):
            return RouteResult(
                kind="hybrid",
                tools=_HYBRID_DEFAULT_TOOLS,
                question=body,
            )

    return RouteResult(kind="llm")


def _extract_service_arg(body: str) -> str:
    for name in ("conveyor-feishu-bot", "conveyor-telegram-bot", "conveyor-maintain.timer"):
        if name in body:
            return name
    return ""


def _extract_log_arg(body: str) -> str:
    m = re.search(r"\b(\d{1,3})\s*(行|lines?)\b", body, re.IGNORECASE)
    if m:
        return m.group(1)
    if "feishu" in body.lower():
        return "conveyor-feishu-bot"
    if "telegram" in body.lower():
        return "conveyor-telegram-bot"
    return ""


def _match_explicit_tool(body: str) -> str | None:
    m = re.match(r"^(?:tool|run\s+tool)\s+(\w+)\s*$", body.strip(), re.IGNORECASE)
    if m and m.group(1) in TOOL_REGISTRY:
        return m.group(1)
    return None


def list_tool_names() -> tuple[str, ...]:
    return tuple(TOOL_REGISTRY.keys())
