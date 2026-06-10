"""handlers/ops.py — deterministic VPS/host ops fast path.

Bypasses Codex entirely. Runs short, sanitised host commands from the
bot process and returns readable text. Channel-agnostic: Telegram and
Feishu share the same code; only the OutboundPort changes.

Safety:
- Uses argument arrays (no shell interpolation of user text).
- 5s timeout per subprocess.
- Never prints env vars, .env, or full process command lines.
- Runs `ps comm` not `args` so secrets in argv are not exposed.
- Output is truncated and run through redact_text.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from typing import Literal

from channel.types import InboundMessage, OutboundPort
from config import Settings
from redaction import redact_text, truncate
from runner import CodexRunner

logger = logging.getLogger(__name__)

OpsKind = Literal["load", "htop", "ps"]


# ---- Intent detection ----------------------------------------------------

# Conservative patterns: high-confidence VPS/host status phrases in
# Chinese and English. We err on the side of false negatives — a coding
# request about "htop in another container" must not be hijacked.
_LOAD_PATTERNS = (
    re.compile(r"(看看|看一下|查一下|看下|查看|查)\s*(我的\s*)?(负载|load|load\s*average)", re.IGNORECASE),
    re.compile(r"(load\s*average|系统负载|机器负载|主机负载)", re.IGNORECASE),
    re.compile(r"(vps|服务器|机器|主机|server)\s*(状态|status|负载|load|情况)", re.IGNORECASE),
    re.compile(r"(状态|status).*(vps|服务器|机器|主机|server)", re.IGNORECASE),
    re.compile(r"(check|show|get)\s+(vps|server|host|system)\s+(status|load|health)", re.IGNORECASE),
    re.compile(r"(show|get)\s+(me\s+)?(the\s+)?(host|server|vps|system)\s+(status|load)", re.IGNORECASE),
)
_HTOP_PATTERNS = (
    re.compile(r"(跑|运行|开|启动|执行|跑一下|跑下)\s*.*\bhtop\b", re.IGNORECASE),
    re.compile(r"(看|查|看一下|看看)\s*.*\bhtop\b", re.IGNORECASE),
    re.compile(r"\bhtop\b\s*(看看|看一下|一下|看看服务器|看看.*vps)", re.IGNORECASE),
    re.compile(r"check\s+htop\s+on\s+(server|vps|host)", re.IGNORECASE),
    re.compile(r"(跑|运行|看|查|执行)\s*.*\btop\b\s*(看一下|看看|一下)?", re.IGNORECASE),
    re.compile(r"\btop\s+(看一下|看看|一下)\b", re.IGNORECASE),
)
_PS_PATTERNS = (
    re.compile(r"(看|查|跑|运行)\s*.*\b(ps|进程|process)", re.IGNORECASE),
    re.compile(r"哪些进程|什么进程|进程占用|进程列表", re.IGNORECASE),
    re.compile(r"\bps\s+aux\b", re.IGNORECASE),
)


def detect_ops_intent(text: str) -> OpsKind | None:
    """Return an ops command kind if the text is clearly a host status
    request, else None. Pure function, no I/O.

    Order matters: htop > ps > load, so "/htop" beats "load" if both
    would match. Calls are first-match-wins; longer/more specific
    patterns come first to avoid eating generic phrases.
    """
    body = (text or "").strip()
    if not body:
        return None
    if "ssh" in body.lower() and re.search(r"\bssh\b.*@\s*\S+", body, re.IGNORECASE):
        # Don't fake credentials for a remote host. Caller will let
        # this fall through to Codex with the natural prompt.
        return None
    for pat in _HTOP_PATTERNS:
        if pat.search(body):
            return "htop"
    for pat in _PS_PATTERNS:
        if pat.search(body):
            return "ps"
    for pat in _LOAD_PATTERNS:
        if pat.search(body):
            return "load"
    return None


# ---- Subprocess helpers --------------------------------------------------

_PER_CMD_TIMEOUT = 5.0


async def _run(argv: list[str], timeout: float = _PER_CMD_TIMEOUT) -> str:
    """Run a command, return stdout (utf-8, lossy). Empty on failure.

    Never raises. Logs the failure for ops debugging.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:
        logger.exception("Failed to spawn %s", argv[0])
        return ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        logger.warning("Subprocess %s timed out after %.1fs", argv[0], timeout)
        return ""
    return (stdout or b"").decode("utf-8", errors="replace").strip()


async def _read_first(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _safe_truncate(text: str, limit: int = 3500) -> str:
    return redact_text(truncate(text, limit))


# ---- Snapshot builders ---------------------------------------------------

async def _load_snapshot() -> str:
    """Return a readable Chinese host snapshot."""
    host = socket.gethostname() or "?"
    now = await _run(["date", "+%Y-%m-%d %H:%M:%S %Z"]) or "?"
    uptime = await _run(["uptime"]) or "?"
    nproc = await _run(["nproc"]) or "?"
    free = await _run(["free", "-h"]) or "(free not available)"
    df_paths = [p for p in ("/", "/srv", "/opt") if os.path.isdir(p)]
    df = (
        await _run(["df", "-h", *df_paths])
        if df_paths
        else "(no /, /srv, /opt)"
    )
    top_cpu = (
        await _run(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%cpu", "ww"])
        or "(ps not available)"
    )
    top_cpu = "\n".join(top_cpu.splitlines()[:11])  # header + 10 rows
    top_mem = (
        await _run(["ps", "-eo", "pid,comm,%cpu,%mem", "--sort=-%mem", "ww"])
        or "(ps not available)"
    )
    top_mem = "\n".join(top_mem.splitlines()[:11])

    sections = [
        "🖥️ VPS / Bot 主机负载快照",
        "",
        f"主机: {host}",
        f"时间: {now}",
        f"运行时间 / 负载: {uptime}",
        f"CPU 核数: {nproc}",
        "",
        "内存:",
        free,
        "",
        "磁盘:",
        df or "(df 不可用)",
        "",
        "CPU 占用最高 (comm, 无 args，避免泄露):",
        top_cpu,
        "",
        "内存占用最高 (comm, 无 args):",
        top_mem,
        "",
        "说明: 这是 bot 服务当前所在机器的本地快照，不是通过 Codex sandbox 猜出来的。",
    ]
    return _safe_truncate("\n".join(sections))


async def _htop_snapshot() -> str:
    """Return a non-interactive htop-style frame.

    htop is a TUI and can't be piped into a chat. Use top -bn1 if
    available; fall back to /load if top is missing.
    """
    top_out = await _run(["top", "-bn1"])
    if not top_out:
        return (
            "htop 是交互式 TUI，这里给你一帧 top/ps 快照。\n"
            "(本机没装 top，下方是 /load 风格的快照。)\n\n"
            + await _load_snapshot()
        )
    # Trim to ~40 lines, drop the header noise but keep task summary.
    lines = top_out.splitlines()
    if len(lines) > 40:
        lines = lines[:40]
    body = _safe_truncate(
        "htop 是交互式 TUI，这里给你一帧 top/ps 快照。\n\n"
        + "\n".join(lines)
    )
    return body


async def _ps_snapshot(*, include_args: bool = False) -> str:
    """Return top processes by CPU and memory.

    Default uses `comm` only. Args mode requires explicit `/ps full confirm`.
    """
    if include_args:
        fmt = "pid,user,comm,args,%cpu,%mem"
        title = "进程快照 (full args 模式，已 redact，但仍可能包含敏感路径/参数):\n\n"
        footer = "\n\n说明: args 可能含 token/路径；已 redact 但不保证完全安全。"
    else:
        fmt = "pid,comm,%cpu,%mem"
        title = "进程快照 (comm 模式，不含 args):\n\n"
        footer = "\n\n说明: 想要 args 视图，发 /ps full confirm（需二次确认）。"
    sort_cpu = await _run(["ps", "-eo", fmt, "--sort=-%cpu"]) or "(ps not available)"
    sort_mem = await _run(["ps", "-eo", fmt, "--sort=-%mem"]) or "(ps not available)"
    body = (
        title
        + "CPU 占用最高:\n"
        + _safe_truncate("\n".join(sort_cpu.splitlines()[:11]))
        + "\n\n内存占用最高:\n"
        + _safe_truncate("\n".join(sort_mem.splitlines()[:11]))
        + footer
    )
    return _safe_truncate(body)


_PS_FULL_WARN = (
    "full args 模式可能包含敏感参数（token、路径、密钥）。\n"
    "请发 /ps full confirm 才显示 args。"
)


def parse_ps_arg(arg: str) -> str:
    """Return 'comm', 'full_warn', or 'full_args'."""
    body = (arg or "").strip().lower()
    if body in ("full confirm", "full confirm=true"):
        return "full_args"
    if body in ("full", "full=true"):
        return "full_warn"
    return "comm"


async def format_ps_output(arg: str) -> str:
    mode = parse_ps_arg(arg)
    if mode == "full_warn":
        return _PS_FULL_WARN
    return await _ps_snapshot(include_args=(mode == "full_args"))


# ---- Command handlers ----------------------------------------------------

async def _load(msg: InboundMessage, port: OutboundPort, _runner, _settings, _arg):
    await port.reply(msg, await _load_snapshot())


async def _htop(msg: InboundMessage, port: OutboundPort, _runner, _settings, _arg):
    await port.reply(msg, await _htop_snapshot())


async def _ps(msg: InboundMessage, port: OutboundPort, _runner, _settings, arg):
    await port.reply(msg, await format_ps_output(arg))


async def _vps(msg: InboundMessage, port: OutboundPort, runner, settings, arg):
    # Friendlier alias for /load.
    await _load(msg, port, runner, settings, arg)


# Dispatch entry used by handlers/dispatch.py for natural-language
# intent (no slash command). Routes to the matching handler above.
async def handle_ops_intent(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    kind: OpsKind,
) -> None:
    if kind == "load":
        await _load(msg, port, runner, settings, "")
    elif kind == "htop":
        await _htop(msg, port, runner, settings, "")
    elif kind == "ps":
        await _ps(msg, port, runner, settings, "")
    else:
        await _load(msg, port, runner, settings, "")