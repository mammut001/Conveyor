"""handlers/tools/executors.py — deterministic tool implementations.

Each executor is async (settings, arg) -> str. Uses argument arrays,
timeouts, redact_text + truncate. Never prints env vars or secrets.
"""
from __future__ import annotations

import json
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

    Integrates live heartbeat status for the desktop node if available.
    """
    import time
    from nodes.registry import list_nodes
    from nodes.types import format_node_block, NodeStatus, NodeType

    nodes = list_nodes(_settings)
    if not nodes:
        return "🖥  执行节点\n\n(没有可用节点)"
    lines = ["🖥  Execution nodes", ""]
    
    desktop_node = None
    for node in nodes:
        lines.append(format_node_block(node))
        if node.node_type == NodeType.DESKTOP:
            desktop_node = node
            if node.status == NodeStatus.ONLINE:
                now = time.time()
                if node.last_seen_at is not None:
                    seconds_ago = max(0, int(now - node.last_seen_at))
                    lines.append(f"  last_seen: {seconds_ago}s ago")
                agent_state = node.metadata.get("agent_state", "idle")
                lines.append(f"  agent_state: {agent_state}")
        lines.append("")

    if desktop_node is not None:
        if desktop_node.status == NodeStatus.OFFLINE:
            lines.append("说明: Desktop node is configured but no fresh heartbeat has been received.")

    return _safe_truncate("\n".join(lines))


def _truncate_display_path(path: str, *, max_len: int = 96) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def _format_latest_screenshot_block(latest: dict | None) -> list[str]:
    if not latest:
        return ["Latest local screenshot: (none yet)"]
    lines = ["Latest local screenshot:"]
    lines.append(f"- id: {latest.get('screenshot_id', '?')}")
    if latest.get("created_at"):
        lines.append(f"- created: {latest['created_at']}")
    width = latest.get("width")
    height = latest.get("height")
    if width is not None and height is not None:
        lines.append(f"- size: {width}x{height}")
    if latest.get("bytes") is not None:
        lines.append(f"- bytes: {latest['bytes']}")
    sha = latest.get("sha256")
    if isinstance(sha, str) and sha:
        lines.append(f"- sha256: {sha[:12]}...")
    if latest.get("path"):
        lines.append(f"- path: {_truncate_display_path(str(latest['path']))}")
    if latest.get("metadata_path"):
        lines.append(f"- metadata: {_truncate_display_path(str(latest['metadata_path']))}")
    return lines


async def _observe_tool_stub(_settings: Settings, _arg: str) -> str:
    return "Observe tool must be invoked via chat routing."


async def exec_desktop_screenshot_status(settings: Settings, _arg: str) -> str:
    """Read-only desktop screenshot observe status (P5.2)."""
    import time
    from desktop_screenshot import (
        helper_configuration_error,
        latest_screenshot_metadata,
        resolve_helper_path,
        resolve_screenshot_dir,
    )
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    from desktop_observe_requests import list_recent_observe_requests

    disclaimer = [
        "This command does not capture a screenshot or upload.",
        "Run `python desktop_agent.py --observe-once` on the Mac for local one-shot capture.",
        "Use /observe_request to create a remote observe request (P5.3+).",
        "Use /observe_preview or natural language like `截图看看我电脑现在是什么` for auto thumbnail delivery (P5.4.3).",
        "Mac agent must run `python desktop_agent.py --poll-observe` to fulfill requests.",
        "P5.4 thumbnail upload is available when CONVEYOR_DESKTOP_UPLOAD_ENABLED=true.",
        "This status command itself does not capture or upload.",
    ]

    lines = [
        "Desktop Screenshot Observe (P5.4)",
        "",
        *disclaimer,
        "",
    ]

    helper_error = helper_configuration_error(settings)
    if helper_error == "screenshot_helper_not_configured":
        lines.extend([
            "Status: helper not configured",
            "",
            "Set CONVEYOR_DESKTOP_SCREENSHOT_HELPER to an absolute path, for example:",
            "  /usr/local/bin/capture-screen-helper",
        ])
        return _safe_truncate("\n".join(lines))

    if helper_error == "screenshot_helper_path_not_absolute":
        lines.extend([
            "Status: helper path must be absolute",
            "",
            "CONVEYOR_DESKTOP_SCREENSHOT_HELPER must be an absolute path.",
            "Relative helper paths are refused for safety.",
        ])
        return _safe_truncate("\n".join(lines))

    helper = resolve_helper_path(settings)
    screenshot_dir = resolve_screenshot_dir(settings)
    lines.extend([
        "Status: helper configured",
        f"Screenshot dir: {_truncate_display_path(str(screenshot_dir))}",
        (
            "Upload: enabled in config but ignored in P5.2"
            if settings.conveyor_desktop_screenshot_allow_upload
            else "Upload: disabled in P5.2"
        ),
        "",
    ])
    if helper is not None:
        lines.append(f"Helper: {_truncate_display_path(str(helper))}")
        lines.append("")

    desktop_nodes = [n for n in list_nodes(settings) if n.node_type == NodeType.DESKTOP]
    if desktop_nodes:
        node = desktop_nodes[0]
        if node.status == NodeStatus.ONLINE:
            last_seen = "unknown"
            if node.last_seen_at is not None:
                last_seen = f"{max(0, int(time.time() - node.last_seen_at))}s ago"
            lines.extend([
                f"Desktop agent: online ({node.node_id})",
                f"Last seen: {last_seen}",
            ])
        else:
            lines.extend([
                f"Desktop agent: offline ({node.node_id})",
                "Heartbeat has not been received recently.",
            ])
    else:
        lines.append("Desktop node: not enabled")
    lines.append("")

    recent = list_recent_observe_requests(settings, limit=3)
    if recent:
        lines.append("Recent observe requests:")
        from handlers.tools.observe_tools import _format_request_summary
        for record in recent:
            lines.extend(_format_request_summary(record))
        lines.append("")

    latest = latest_screenshot_metadata(settings)
    lines.extend(_format_latest_screenshot_block(latest))
    return _safe_truncate("\n".join(lines))


def _format_loop_result(settings: Settings, result: dict) -> str:
    if not result.get("ok"):
        return f"❌ {result.get('error')}: {result.get('message')}"
    status = result.get("status")
    summary = result.get("summary")
    task_id = result.get("task_id")
    
    is_failure = (status != "done") or (summary == "max_steps reached")
    if is_failure:
        from desktop_computer_requests import get_computer_task
        task = get_computer_task(settings, task_id) or {}
        trajectory = task.get("trajectory", []) or []
        
        reason = result.get("blocked_reason") or summary or status
        
        last_action_str = "None"
        if trajectory:
            last = trajectory[-1]
            action_type = last.get("action_type")
            redacted = last.get("action_redacted") or {}
            args = {k: v for k, v in redacted.items() if k != "action"}
            args_str = json.dumps(args, ensure_ascii=False) if args else ""
            last_action_str = f"{action_type} {args_str}".strip()
            
        last_screenshot_id = "None"
        last_screenshot_hash = "None"
        for step in reversed(trajectory):
            if step.get("screenshot_id"):
                last_screenshot_id = step.get("screenshot_id")
                last_screenshot_hash = step.get("screenshot_hash") or "None"
                break
                
        lines = [
            f"❌ Task failed/stopped",
            f"Task ID: {task_id}",
            f"Stop Reason: {reason}",
            f"Last Action: {last_action_str}",
            f"Last Screenshot: {last_screenshot_id} (hash: {last_screenshot_hash})",
            f"Steps Completed: {result.get('steps_used', 0)}",
            f"Suggestion: Run `/computer_log {task_id}` to view details.",
        ]
        return _safe_truncate("\n".join(lines))
        
    lines = [f"🖥 Computer Use 任务 {task_id}", ""]
    lines.append(f"状态: {status}")
    lines.append(f"步数: {result.get('steps_used')}")
    if summary:
        lines.append(f"摘要: {summary}")
    if result.get("blocked_reason"):
        lines.append(f"停止原因: {result.get('blocked_reason')}")
    return _safe_truncate("\n".join(lines))


async def exec_computer_status(settings: Settings, _arg: str) -> str:
    """P5.6 Computer Use status: enabled flag, direct-mode source, Cua probe."""
    from desktop_computer_requests import (
        arm_remaining_seconds,
        direct_mode_source,
        get_active_task,
        is_direct_mode_active,
        _parse_iso,
        _utc_now,
    )
    from desktop_cua import probe_cua_driver
    from nodes.state import get_desktop_runtime, is_desktop_online
    import time

    enabled = settings.conveyor_computer_use_enabled
    source = direct_mode_source(settings) if enabled else None
    lines = ["🖥 Computer Use (P5.6 Direct)", ""]
    lines.append(f"启用: {'是' if enabled else '否 (CONVEYOR_COMPUTER_USE_ENABLED=false)'}")
    
    # Direct mode state
    direct_active = is_direct_mode_active(settings)
    lines.append(f"Direct 模式: {'启用' if direct_active else '未启用'}")
    if source:
        lines.append(f"Direct 模式来源: {source}")
        if source == "armed":
            lines.append(f"剩余 TTL: {arm_remaining_seconds(settings)}s")
    lines.append(f"Always-Direct: {'是' if settings.conveyor_computer_always_direct else '否'}")
    lines.append(
        f"Max steps: {settings.conveyor_computer_max_steps}, "
        f"Max seconds: {settings.conveyor_computer_max_seconds}"
    )
    
    # Cua driver probe
    lines.append(f"Cua driver command: {settings.conveyor_cua_driver_cmd}")
    if enabled:
        probe = probe_cua_driver(settings.conveyor_cua_driver_cmd, settings=settings)
        if probe.get("available"):
            lines.append(f"Cua driver: 可用 ({probe.get('path')})")
            if probe.get("version"):
                lines.append(f"Cua driver version: {probe.get('version')}")
            perms = probe.get("permissions")
            if isinstance(perms, dict):
                status = perms.get("status") or "unknown"
                accessibility = perms.get("accessibility")
                screen_rec = perms.get("screen_recording")
                daemon = perms.get("daemon_running")
                daemon_text = "yes" if daemon is True else "no" if daemon is False else "unknown"
                lines.append(f"Cua permissions: {status} (daemon: {daemon_text})")
                lines.append(f"Accessibility: {accessibility}, Screen Recording: {screen_rec}")
                reason = perms.get("reason")
                if status != "granted" and isinstance(reason, str) and reason.strip():
                    lines.append(f"Cua permissions note: {reason[:320]}")
        else:
            lines.append(f"Cua driver: 未找到 ({probe.get('error')}) — 仅 Mac 端需要")

    # App allowlist / blocklist
    allowed_apps = getattr(settings, "conveyor_computer_allowed_apps", ())
    blocked_apps = getattr(settings, "conveyor_computer_blocked_apps", ())
    lines.append(f"Allowed apps: {', '.join(allowed_apps) if allowed_apps else '无限制'}")
    lines.append(f"Blocked apps: {', '.join(blocked_apps) if blocked_apps else '无'}")

    # Node heartbeat state
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    node_state = get_desktop_runtime(settings, node_id)
    online = is_desktop_online(settings, node_id)
    if node_state:
        poll_comp = node_state.get("poll_computer", False)
        last_seen = node_state.get("last_seen_at")
        elapsed = int(time.time() - last_seen) if last_seen else -1
        lines.append(f"Desktop agent poll-computer: {'enabled' if poll_comp else 'disabled'}")
        if elapsed >= 0:
            lines.append(f"Desktop last heartbeat: {elapsed}s ago (online: {online})")
        else:
            lines.append("Desktop last heartbeat: Never")
    else:
        lines.append("Desktop agent heartbeat: No heartbeat received yet")

    # Active task status
    active = get_active_task(settings)
    lines.append("")
    if active:
        task_id = active.get("task_id")
        created_at_dt = _parse_iso(active.get("created_at"))
        elapsed_sec = int((_utc_now() - created_at_dt).total_seconds()) if created_at_dt else 0
        
        trajectory = active.get("trajectory", []) or []
        last_action_str = "None"
        if trajectory:
            last = trajectory[-1]
            last_action_str = last.get("action_type")
            
        lines.append(f"运行中任务: {task_id}")
        lines.append(f"目标: {active.get('goal')}")
        lines.append(f"已执行步数: {active.get('step_seq', 0)}")
        lines.append(f"已运行时间: {elapsed_sec}s")
        lines.append(f"最后动作: {last_action_str}")
    else:
        lines.append("运行中任务: 无")
        
    return _safe_truncate("\n".join(lines))


async def exec_computer_observe(settings: Settings, arg: str) -> str:
    """Trigger one desktop observation (screenshot/state metadata)."""
    from desktop_computer_requests import (
        contains_blocked_keyword,
        is_direct_mode_active,
    )
    from desktop_computer_planner import ScriptedPlanner
    from desktop_computer_loop import build_backend, run_computer_loop

    if not settings.conveyor_computer_use_enabled:
        return "⚠️ Computer Use 未启用 (CONVEYOR_COMPUTER_USE_ENABLED=false)。"
    if contains_blocked_keyword(settings, arg or ""):
        return "⛔ 含受限关键词，已拒绝。"
    if not is_direct_mode_active(settings):
        return "⚠️ Direct 模式未启用。先 /computer_arm [分钟] 或设置 CONVEYOR_COMPUTER_ALWAYS_DIRECT=true。"
    planner = ScriptedPlanner([{"action": "observe"}, {"action": "done", "summary": "observed"}])
    backend = build_backend(settings)
    result = await run_computer_loop(
        settings, "observe desktop", planner=planner, backend=backend,
        max_steps=2, max_seconds=30, direct_mode=True,
    )
    if not result.get("ok"):
        return _format_loop_result(settings, result)
    # Surface the last observation metadata from the trajectory.
    from desktop_computer_requests import get_computer_task
    task = get_computer_task(settings, result["task_id"]) or {}
    last = None
    for entry in task.get("trajectory", []):
        if isinstance(entry, dict) and entry.get("screenshot_id"):
            last = entry
    if last:
        return _safe_truncate(
            f"🖥 桌面观察完成\n\n"
            f"screenshot_id: {last.get('screenshot_id')}\n"
            f"动作步数: {result.get('steps_used')}"
        )
    return _format_loop_result(settings, result)


async def exec_computer_action(settings: Settings, arg: str) -> str:
    """Execute a single desktop action from a JSON action payload."""
    from desktop_computer_requests import (
        append_trajectory,
        contains_blocked_keyword,
        create_computer_step,
        create_computer_task,
        is_action_allowed,
        is_direct_mode_active,
        normalize_action,
        redact_computer_action,
        set_task_status,
    )
    from desktop_computer_loop import build_backend

    if not settings.conveyor_computer_use_enabled:
        return "⚠️ Computer Use 未启用 (CONVEYOR_COMPUTER_USE_ENABLED=false)。"
    if not is_direct_mode_active(settings):
        return "⚠️ Direct 模式未启用。先 /computer_arm [分钟] 或设置 CONVEYOR_COMPUTER_ALWAYS_DIRECT=true。"
    try:
        action = json.loads(arg)
    except Exception:
        return '参数需为 JSON action，例如: {"action":"click","x":100,"y":100}'
    if not isinstance(action, dict) or "action" not in action:
        return "缺少 action 字段。"
    if not is_action_allowed(settings, action):
        return f"不允许的动作: {action.get('action')}"
    if contains_blocked_keyword(settings, json.dumps(action)):
        return "⛔ 含受限关键词，已拒绝。"

    backend = build_backend(settings)
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    created = create_computer_task(
        settings, "single action", direct_mode=True,
        max_steps=1, max_seconds=30, operator_id="", chat_id="", channel="",
    )
    if not created.get("ok"):
        return _format_loop_result(settings, created)
    task_id = created["task_id"]
    step = create_computer_step(settings, task_id, action)
    step_id = step["step_id"]
    try:
        result = await backend.execute_step(settings, task_id, step_id, normalize_action(action))
        append_trajectory(settings, task_id, {
            "action_type": action.get("action"),
            "action_redacted": redact_computer_action(action),
            "result_ok": True,
            "screenshot_id": (result or {}).get("screenshot_id"),
        })
        set_task_status(settings, task_id, "done", summary="single action executed")
        res_str = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
        return _safe_truncate(f"✅ 动作已执行: {action.get('action')}\n结果: {res_str}")
    except Exception as exc:
        set_task_status(settings, task_id, "error", blocked_reason=str(exc))
        return f"动作执行失败: {type(exc).__name__}"


async def exec_computer_task(settings: Settings, arg: str) -> str:
    """Run the Codex action loop to complete a desktop goal (hands-free)."""
    from desktop_computer_requests import (
        contains_blocked_keyword,
        is_direct_mode_active,
    )
    from desktop_computer_planner import CodexPlanner
    from desktop_computer_loop import build_backend, run_computer_loop

    if not settings.conveyor_computer_use_enabled:
        return "⚠️ Computer Use 未启用 (CONVEYOR_COMPUTER_USE_ENABLED=false)。"
    goal = (arg or "").strip()
    if not goal:
        return "用法: /computer_task <目标>\n例如: /computer_task 打开 Chrome 并访问 conveyor.dev"
    if contains_blocked_keyword(settings, goal):
        return "⛔ 目标含受限关键词，已拒绝执行任何桌面动作。"
    if not is_direct_mode_active(settings):
        return (
            "⚠️ Direct 模式未启用，无法自动执行。\n"
            "先 /computer_arm [分钟] 启用(或设置 CONVEYOR_COMPUTER_ALWAYS_DIRECT=true)。"
        )
    planner = CodexPlanner(settings)
    backend = build_backend(settings)
    result = await run_computer_loop(
        settings, goal, planner=planner, backend=backend,
        max_steps=settings.conveyor_computer_max_steps,
        max_seconds=settings.conveyor_computer_max_seconds,
        direct_mode=True,
    )
    return _format_loop_result(settings, result)


async def exec_computer_stop(settings: Settings, _arg: str) -> str:
    """Immediately cancel the active Computer Use task."""
    from desktop_computer_requests import cancel_computer_task, get_active_task

    if not settings.conveyor_computer_use_enabled:
        return "Computer Use 未启用。"
    task = get_active_task(settings)
    if not task:
        return "没有正在运行的 Computer Use 任务。"
    res = cancel_computer_task(settings, task["task_id"], reason="operator_stop")
    if res.get("ok"):
        return f"🛑 已取消任务 {task['task_id']}。"
    return f"取消失败: {res.get('error')}"


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
            name="desktop.screenshot.status",
            summary="Desktop screenshot observe 状态 (P5.2 read-only)",
            danger=DangerLevel.READ,
            executor=exec_desktop_screenshot_status,
            keywords=("screenshot observe", "桌面截图", "截屏状态", "screenshot status"),
        ),
        ToolSpec(
            name="desktop.observe.request",
            summary="Create remote desktop observe request (P5.3 metadata only)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("observe request", "request screenshot", "remote screenshot"),
        ),
        ToolSpec(
            name="desktop.observe.status",
            summary="Recent observe requests and screenshot metadata (P5.3)",
            danger=DangerLevel.READ,
            executor=_observe_tool_stub,
            keywords=("observe status", "observe requests"),
        ),
        ToolSpec(
            name="desktop.observe.cancel",
            summary="Cancel pending/claimed observe request (P5.3)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("cancel observe", "observe cancel"),
        ),
        ToolSpec(
            name="desktop.upload.request",
            summary="Create remote desktop thumbnail upload request (P5.4)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("upload request", "request upload", "observe upload", "screenshot upload"),
        ),
        ToolSpec(
            name="desktop.upload.status",
            summary="Recent upload requests and thumbnail status (P5.4)",
            danger=DangerLevel.READ,
            executor=_observe_tool_stub,
            keywords=("upload status", "upload requests"),
        ),
        ToolSpec(
            name="desktop.upload.cancel",
            summary="Cancel pending/claimed upload request (P5.4)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("cancel upload", "upload cancel"),
        ),
        ToolSpec(
            name="desktop.upload.resend",
            summary="Resend a completed thumbnail upload to chat (P5.4.2)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("resend upload", "upload resend", "retry upload"),
        ),
        ToolSpec(
            name="desktop.upload.cleanup",
            summary="Clean up VPS temporary uploaded thumbnails (P5.4)",
            danger=DangerLevel.WRITE_SAFE,
            executor=_observe_tool_stub,
            keywords=("upload cleanup", "cleanup upload"),
        ),
        ToolSpec(
            name="computer.status",
            summary="Computer Use (desktop agent) 状态 — 含 Direct 模式",
            danger=DangerLevel.READ,
            executor=exec_computer_status,
            keywords=("computer use", "桌面", "desktop status", "截屏 status", "direct mode"),
        ),
        ToolSpec(
            name="computer.observe",
            summary="触发一次桌面观察 (screenshot/state 元数据)",
            danger=DangerLevel.READ,
            executor=exec_computer_observe,
            keywords=("computer observe", "桌面观察", "看一眼电脑", "observe desktop"),
        ),
        ToolSpec(
            name="computer.action",
            summary="执行单个桌面动作 (click/type/hotkey/scroll/wait)",
            danger=DangerLevel.WRITE_SAFE,
            executor=exec_computer_action,
            keywords=("computer action", "桌面动作", "点一下", "敲一下"),
        ),
        ToolSpec(
            name="computer.task",
            summary="运行 Codex 动作循环完成桌面目标 (需授权)",
            danger=DangerLevel.WRITE,
            executor=exec_computer_task,
            keywords=("computer task", "操作电脑", "帮我点", "打开", "在电脑上"),
        ),
        ToolSpec(
            name="computer.stop",
            summary="立即取消正在运行的 Computer Use 任务",
            danger=DangerLevel.WRITE_SAFE,
            executor=exec_computer_stop,
            keywords=("computer stop", "停止操作", "取消电脑任务", "stop computer"),
        ),
    ]
    for spec in specs:
        register_tool(spec)


register_builtin_tools()
