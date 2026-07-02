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


async def exec_scheduler_status(_settings: Settings, _arg: str) -> str:
    from scripts.scheduler_probe import scheduler_status_report
    return scheduler_status_report()


async def exec_scheduler_probe(_settings: Settings, _arg: str) -> str:
    from scripts.scheduler_probe import scheduler_probe_dry_run
    return scheduler_probe_dry_run()


async def exec_scheduler_probe_live(_settings: Settings, _arg: str) -> str:
    from scripts.scheduler_probe import scheduler_probe_live
    return scheduler_probe_live()


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


async def exec_queue_status(_settings: Settings, _arg: str) -> str:
    """Return Codex job queue status."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    return await queue.get_queue_status()


async def exec_nodes_status(_settings: Settings, _arg: str) -> str:
    """List known execution nodes and their capabilities.

    Phase 0 read-only snapshot. The VPS node is always listed.
    The desktop node is listed only when the operator opted in
    via ``CONVEYOR_DESKTOP_NODE_ENABLED`` — and it is offline
    regardless, because no local agent is wired up in this task.
    """
    from nodes.registry import list_nodes
    from nodes.types import format_node_block

    nodes = list_nodes(_settings)
    if not nodes:
        return "🖥  执行节点\n\n(没有可用节点)"
    lines = ["🖥  Execution nodes", ""]
    for node in nodes:
        lines.append(format_node_block(node))
        lines.append("")
    lines.append(
        "说明: VPS 是当前控制平面; Desktop 节点为 stub, 真实截屏/鼠标/键盘/"
        "Computer Use 仍是未来工作。"
    )
    return _safe_truncate("\n".join(lines))


async def exec_computer_status(_settings: Settings, _arg: str) -> str:
    """Stub tool for Computer Use requests.

    Real desktop control is not implemented in this task. The
    tool exists so natural-language phrases like
    ``帮我在 Mac 上打开 Xcode`` route to a deterministic
    stub reply instead of falling through to Codex (which
    cannot see the operator's laptop from the VPS).
    """
    from nodes.registry import is_stub_environment, list_nodes

    if is_stub_environment(_settings):
        desktop_nodes = [
            n for n in list_nodes(_settings) if n.node_type.value == "desktop"
        ]
        if not desktop_nodes:
            body = (
                "🖥  Computer Use: 未启用\n\n"
                "Desktop 节点未在 .env 中启用 (CONVEYOR_DESKTOP_NODE_ENABLED=false)。\n"
                "当前没有触发任何桌面动作。\n\n"
                "下一步: 在 .env 里把 CONVEYOR_DESKTOP_NODE_ENABLED 设为 true，"
                "再部署一个本地 desktop agent（未来工作）。"
            )
        else:
            node = desktop_nodes[0]
            body = (
                f"🖥  Computer Use: 已配置但未运行\n\n"
                f"节点: {node.node_id} · {node.display_name}\n"
                f"状态: {node.status.value}\n"
                f"模式: {node.metadata.get('computer_use_mode', 'observe_only')}\n\n"
                "真实截屏 / 鼠标 / 键盘 / 浏览器控制 / Gemini Computer Use "
                "**尚未实现**。\n"
                "当前仅显示节点配置信息，没有触发任何桌面动作。\n\n"
                "下一步: 部署一个本地 desktop agent，然后让它向本服务发送 "
                "heartbeat (未来工作)。"
            )
        return _safe_truncate(body)

    # Future branch: when is_stub_environment flips to False, this
    # tool can return a real status from the agent's heartbeat.
    # Left as an explicit dead branch so a future implementer
    # does not silently fall through to a default.
    return _safe_truncate(
        "🖥  Computer Use: unknown\n\n"
        "Stub 环境标志为 False 但未实现真实状态读取。请更新 is_stub_environment "
        "或实现 heart beat 路径。"
    )


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
        ToolSpec(
            name="scheduler_status",
            summary="提醒调度器状态报告",
            danger=DangerLevel.READ,
            executor=exec_scheduler_status,
            keywords=("scheduler", "调度器", "提醒状态"),
        ),
        ToolSpec(
            name="scheduler_probe",
            summary="调度器 dry-run 探测",
            danger=DangerLevel.READ,
            executor=exec_scheduler_probe,
            keywords=("probe", "探针", "dry-run"),
        ),
        ToolSpec(
            name="scheduler_probe_live",
            summary="调度器实时投递测试 (需确认)",
            danger=DangerLevel.WRITE,
            executor=exec_scheduler_probe_live,
            keywords=("probe live", "实时探针"),
        ),
        ToolSpec(
            name="queue.status",
            summary="Codex 任务队列状态",
            danger=DangerLevel.READ,
            executor=exec_queue_status,
            keywords=("队列", "queue", "任务队列"),
        ),
        # P5.0: Execution-node layer. The desktop node is a stub
        # in this task — see ``docs/desktop_security.md``.
        ToolSpec(
            name="nodes.status",
            summary="Execution nodes (VPS + desktop stub) 状态",
            danger=DangerLevel.READ,
            executor=exec_nodes_status,
            keywords=("节点", "nodes", "host status", "vps + desktop"),
        ),
        ToolSpec(
            name="computer.status",
            summary="Computer Use (desktop agent) 状态 — 当前为 stub",
            danger=DangerLevel.READ,
            executor=exec_computer_status,
            keywords=("computer use", "桌面", "desktop status", "截屏 status"),
        ),
    ]
    for spec in specs:
        register_tool(spec)


register_builtin_tools()
