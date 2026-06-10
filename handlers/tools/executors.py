"""handlers/tools/executors.py — deterministic tool implementations.

Each executor is async (settings, arg) -> str. Uses argument arrays,
timeouts, redact_text + truncate. Never prints env vars or secrets.
"""
from __future__ import annotations

import logging
import os
import socket

from config import Settings
from handlers.ops import _load_snapshot, _ps_snapshot, _htop_snapshot, _run, _safe_truncate
from handlers.tools.registry import DangerLevel, ToolSpec, register_tool

logger = logging.getLogger(__name__)

_CONVEYOR_SERVICES = (
    "conveyor-telegram-bot",
    "conveyor-feishu-bot",
    "conveyor-maintain.timer",
)


async def exec_load(_settings: Settings, _arg: str) -> str:
    return await _load_snapshot()


async def exec_ps(_settings: Settings, arg: str) -> str:
    from handlers.ops import format_ps_output
    return await format_ps_output(arg)


async def exec_disk(_settings: Settings, _arg: str) -> str:
    host = socket.gethostname() or "?"
    df_paths = [p for p in ("/", "/srv", "/opt") if os.path.isdir(p)]
    df = (
        await _run(["df", "-h", *df_paths])
        if df_paths
        else "(no mount points)"
    )
    body = (
        "💾 磁盘使用快照\n\n"
        f"主机: {host}\n\n"
        f"{df or '(df 不可用)'}\n\n"
        "说明: bot 服务所在机器的本地 df 输出。"
    )
    return _safe_truncate(body)


async def exec_logs(_settings: Settings, arg: str) -> str:
    """Tail recent journal lines for conveyor systemd units."""
    limit = 30
    if arg.strip().isdigit():
        limit = max(5, min(100, int(arg.strip())))
    service = _CONVEYOR_SERVICES[0]
    if arg.strip() and not arg.strip().isdigit():
        candidate = arg.strip().split()[0]
        if candidate in _CONVEYOR_SERVICES or candidate.startswith("conveyor"):
            service = candidate
    lines = await _run(
        [
            "journalctl",
            "-u",
            service,
            "-n",
            str(limit),
            "--no-pager",
            "-o",
            "short-iso",
        ],
        timeout=8.0,
    )
    if not lines:
        lines = "(journalctl 不可用或无日志)"
    body = (
        f"📋 服务日志 ({service}, 最近 {limit} 行)\n\n"
        f"{_safe_truncate(lines, 3000)}\n\n"
        "说明: 来自 bot 主机 journalctl，已 redact。"
    )
    return _safe_truncate(body)


async def exec_service_status(_settings: Settings, _arg: str) -> str:
    sections: list[str] = ["🔧 Conveyor 服务状态\n"]
    for unit in _CONVEYOR_SERVICES:
        active = await _run(["systemctl", "is-active", unit]) or "unknown"
        enabled = await _run(["systemctl", "is-enabled", unit]) or "unknown"
        sections.append(f"  {unit}: active={active}, enabled={enabled}")
    sections.append("\n说明: bot 主机 systemd 本地查询。")
    return _safe_truncate("\n".join(sections))


async def exec_git_status(settings: Settings, _arg: str) -> str:
    root = settings.codex_workspace_root
    if not root.is_dir():
        return f"工作区不存在: {root}"
    branch = await _run(["git", "-C", str(root), "branch", "--show-current"]) or "?"
    status = await _run(["git", "-C", str(root), "status", "--short"]) or "(clean)"
    diff_stat = await _run(["git", "-C", str(root), "diff", "--stat"]) or ""
    body = (
        "📦 Git 状态\n\n"
        f"路径: {root}\n"
        f"分支: {branch}\n\n"
        "变更文件:\n"
        f"{status}\n"
    )
    if diff_stat.strip():
        body += f"\nDiff stat:\n{diff_stat}\n"
    body += "\n说明: workspace 本地 git，不含未追踪 secret 文件内容。"
    return _safe_truncate(body)


async def exec_htop(_settings: Settings, _arg: str) -> str:
    return await _htop_snapshot()


# Friendly restart aliases used by both /restart and the natural-language
# intent router. Mapping is identical to handlers/tools/restart_aliases.py
# but duplicated here intentionally so executors.py has no upward
# dependency on the intent layer (executors must stay callable from
# any entry point, not just /restart).
RESTART_ALIASES: dict[str, str] = {
    "telegram": "conveyor-telegram-bot",
    "feishu": "conveyor-feishu-bot",
    "maintain": "conveyor-maintain.timer",
}

# Chinese-friendly aliases that resolve to the same units.
RESTART_ALIASES_ZH: dict[str, str] = {
    "飞书": "conveyor-feishu-bot",
    "电报": "conveyor-telegram-bot",
    "tg": "conveyor-telegram-bot",
    "维护": "conveyor-maintain.timer",
}


async def exec_service_restart(_settings: Settings, arg: str) -> str:
    """Dangerous: restart a conveyor systemd unit. Requires confirmation.

    No implicit default: an empty or unknown arg is refused without
    touching systemctl. Natural-language intent must resolve to a
    concrete unit before reaching this executor; ambiguous requests
    get a usage reply instead.
    """
    raw = (arg or "").strip().lower()
    if not raw:
        return (
            "未指定可重启的服务。请使用 /restart telegram|feishu|maintain，"
            "或在自然语言里明确写目标，例如「重启 feishu bot」「重启 telegram 服务」。"
        )
    unit = RESTART_ALIASES.get(raw) or RESTART_ALIASES_ZH.get(raw) or raw
    if unit not in _CONVEYOR_SERVICES:
        allowed = ", ".join(_CONVEYOR_SERVICES)
        return f"只允许重启以下服务: {allowed}\n（未知单元 {unit!r} 已拒绝）"
    result = await _run(["sudo", "systemctl", "restart", unit], timeout=15.0)
    active = await _run(["systemctl", "is-active", unit]) or "unknown"
    body = (
        f"🔄 已请求重启 {unit}\n"
        f"当前状态: active={active}\n"
    )
    if result:
        body += f"\n{result}\n"
    return _safe_truncate(body)


def register_builtin_tools() -> None:
    """Populate TOOL_REGISTRY. Called once at import."""
    specs = [
        ToolSpec(
            name="load",
            summary="主机负载/内存/CPU/进程快照",
            danger=DangerLevel.READ,
            executor=exec_load,
            keywords=("负载", "load", "vps", "服务器状态", "host status"),
        ),
        ToolSpec(
            name="ps",
            summary="Top 进程 (comm 模式，默认不含 args)",
            danger=DangerLevel.READ,
            executor=exec_ps,
            keywords=("进程", "process", "ps aux"),
        ),
        ToolSpec(
            name="htop",
            summary="top 一帧快照 (htop 是 TUI)",
            danger=DangerLevel.READ,
            executor=exec_htop,
            keywords=("htop", "top 看一下"),
        ),
        ToolSpec(
            name="disk",
            summary="磁盘使用 (/ /srv /opt)",
            danger=DangerLevel.READ,
            executor=exec_disk,
            keywords=("磁盘", "disk", "df", "空间"),
        ),
        ToolSpec(
            name="logs",
            summary="Conveyor 服务 journal 日志",
            danger=DangerLevel.READ,
            executor=exec_logs,
            keywords=("日志", "log", "journal"),
        ),
        ToolSpec(
            name="service_status",
            summary="Conveyor systemd 服务状态",
            danger=DangerLevel.READ,
            executor=exec_service_status,
            keywords=("服务状态", "service status", "systemctl", "还在跑"),
        ),
        ToolSpec(
            name="git_status",
            summary="Workspace git status",
            danger=DangerLevel.READ,
            executor=exec_git_status,
            keywords=("git status", "代码改动", "改了什么"),
        ),
        ToolSpec(
            name="service_restart",
            summary="重启 Conveyor systemd 服务 (需确认)",
            danger=DangerLevel.WRITE,
            executor=exec_service_restart,
            keywords=("重启", "restart service", "restart bot"),
        ),
    ]
    for spec in specs:
        register_tool(spec)


register_builtin_tools()
