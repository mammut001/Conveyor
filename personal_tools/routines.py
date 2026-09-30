"""personal_tools/routines.py — Chat tools for managing scheduled routines."""
from __future__ import annotations

from typing import Any

from config import Settings
from personal_tools.base import ToolResult
import routines


async def routine_list(
    settings: Settings,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """List configured routines."""
    items = routines.list_routines(settings)
    if not items:
        return ToolResult(True, "暂无配置的定时任务。")

    lines = [f"定时任务列表 ({len(items)}):"]
    for r in items:
        status_icon = "🟢" if r.get("enabled") else "⏸️"
        next_str = routines.format_local(settings, r.get("next_run_at"))
        last_status = r.get("last_run_status") or "no runs"
        lines.append(
            f"{status_icon} #{r['id']} · {r['name']} · cron: {r['schedule_cron']} · next: {next_str} · last: {last_status}"
        )
    return ToolResult(True, "\n".join(lines))


async def routine_create(
    settings: Settings,
    arg: str,
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Create a routine. Format: <5-field cron> | <prompt> [| <name>]."""
    parts = [p.strip() for p in (arg or "").split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return ToolResult(False, "用法: routine.create <5-field cron> | <prompt> [| <name>]")

    cron_expr = parts[0]
    prompt = parts[1]
    name = parts[2] if len(parts) >= 3 and parts[2] else prompt[:30].strip()

    origin_channel = channel or "web"
    origin_chat_id = chat_id or ""
    if origin_channel == "telegram":
        deliver = ["web", "telegram"]
    elif origin_channel == "feishu":
        deliver = ["web", "feishu"]
    else:
        deliver = ["web"]

    try:
        r = routines.create_routine(
            settings,
            name=name,
            schedule=cron_expr,
            prompt=prompt,
            deliver=deliver,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            enabled=True,
        )
        return ToolResult(
            True,
            f"Routine #{r['id']} ('{r['name']}') created: {r['schedule_cron']} — next run "
            f"{routines.format_local(settings, r['next_run_at'])}",
        )
    except Exception as exc:
        return ToolResult(False, f"创建定时任务失败: {exc}")


async def routine_pause(
    settings: Settings,
    arg: str,
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Pause a routine. Usage: routine.pause <id>."""
    raw = (arg or "").strip()
    if not raw.isdigit():
        return ToolResult(False, "用法: routine.pause <id>")

    r = routines.pause_routine(settings, int(raw))
    if not r:
        return ToolResult(False, f"未找到定时任务 #{raw}")
    return ToolResult(True, f"Routine #{raw} ('{r['name']}') paused.")


async def routine_resume(
    settings: Settings,
    arg: str,
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Resume a routine. Usage: routine.resume <id>."""
    raw = (arg or "").strip()
    if not raw.isdigit():
        return ToolResult(False, "用法: routine.resume <id>")

    r = routines.resume_routine(settings, int(raw))
    if not r:
        return ToolResult(False, f"未找到定时任务 #{raw}")
    return ToolResult(True, f"Routine #{raw} ('{r['name']}') resumed — next run {routines.format_local(settings, r['next_run_at'])}")


async def routine_delete(
    settings: Settings,
    arg: str,
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Delete a routine. Usage: routine.delete <id>."""
    raw = (arg or "").strip()
    if not raw.isdigit():
        return ToolResult(False, "用法: routine.delete <id>")

    existing = routines.get_routine(settings, int(raw))
    name = existing["name"] if existing else f"#{raw}"
    ok = routines.delete_routine(settings, int(raw))
    if not ok:
        return ToolResult(False, f"未找到定时任务 #{raw}")
    return ToolResult(True, f"Routine #{raw} ('{name}') deleted.")


async def routine_run(
    settings: Settings,
    arg: str,
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Run a routine now. Usage: routine.run <id>."""
    raw = (arg or "").strip()
    if not raw.isdigit():
        return ToolResult(False, "用法: routine.run <id>")

    # Only queue it: the web console executes routines (it holds their approvals).
    r = routines.request_run_now(settings, int(raw))
    if not r:
        return ToolResult(False, f"未找到已启用的定时任务 #{raw}")
    return ToolResult(
        True,
        f"Routine #{raw} ('{r['name']}') queued; the Web Console will run it within ~30s "
        "and post the result to its inbox.",
    )
