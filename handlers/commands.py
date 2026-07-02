"""handlers/commands.py — channel-agnostic command dispatch (003 P1).

Single source of truth for every /<cmd> the bot supports. Each
spec receives an InboundMessage + OutboundPort and produces
zero or more replies. Telegram and Feishu adapters call
`run_command()` after parsing; neither adapter hard-codes a list.

Adapters that don't support a given command simply skip its
entry from their set_my_commands / help text.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from channel.types import InboundMessage, OutboundPort
from config import Settings
from redaction import truncate
from runner import CodexRunner
from scripts.auto_maintain import run_maintenance
from scripts.diagnostics import diagnostics_report
from scripts.doctor import (
    check_disk,
    check_latest_job,
    check_runtime_dirs,
    check_workspace,
)
from scripts.harness_common import check_minimax_models, check_systemd_active
from scripts.job_audit import run_job_audit
from scripts.log_summary import summarize_log
from scripts.metadata_report import metadata_report
from scripts.metrics_report import metrics_report
from scripts.rate_limit_report import rate_limit_report
from scripts.security_audit import run_security_audit
from scripts.smoke import run_smoke
from scripts.edit_harness import run_edit_harness
from scripts.health_snapshot import health_snapshot
from handlers import ops as ops_handlers
from handlers.tools.registry import TOOL_REGISTRY, DangerLevel
from handlers.tools.restart_aliases import RESTART_USAGE, resolve_restart_alias

# Mirrors runner-side constants; keep near command logic that needs them.
DATE_ARG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CommandHandler = Callable[[InboundMessage, OutboundPort, CodexRunner, Settings, str], Awaitable[None]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    handler: CommandHandler
    takes_arg: bool = False
    # Some commands take an optional arg (jobs, audit, diag, log, etc.).
    takes_optional_arg: bool = False


# ---- Helpers --------------------------------------------------------------

def _int_arg(arg: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(arg)))
    except (TypeError, ValueError):
        return default


def _join_arg(arg: str) -> str:
    """Arg passed by the dispatcher is whitespace-trimmed; rejoin to
    allow commands like `/diag 30 min` to use multi-word since-clauses."""
    return arg.strip()


# ---- Built-in commands ----------------------------------------------------

async def _status(msg, port, runner, _settings, _arg):
    await port.reply(msg, runner.status_text())


async def _last(msg, port, runner, _settings, _arg):
    await port.reply(msg, runner.last_text())


async def _cancel(msg, port, runner, _settings, _arg):
    await port.reply(msg, await runner.cancel())


async def _diff(msg, port, runner, _settings, _arg):
    full_diff = await runner.diff_text()
    # On Feishu, send a structured card (Apply / Discard / Status)
    # as a fresh message, then send the full diff as a follow-up
    # text reply. Telegram still gets the original text-only path.
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        try:
            from channel.feishu_cards import diff_preview_card
            await port.send_card(msg, diff_preview_card(
                job_id=str(getattr(getattr(runner, "current_job", None), "id", "") or ""),
                diff_summary=full_diff,
            ))
        except Exception:
            pass
    await port.reply(msg, full_diff)


async def _discard(msg, port, runner, _settings, _arg):
    await port.reply(msg, await runner.discard_last_job())


async def _apply(msg, port, runner, _settings, _arg):
    await port.reply(msg, await runner.apply_last_job())


async def _jobs(msg, port, runner, _settings, arg):
    limit = _int_arg(arg, default=8, lo=1, hi=30)
    await port.reply(msg, runner.jobs_text(limit))


async def _clean(msg, port, runner, _settings, arg):
    keep = _int_arg(arg, default=20, lo=1, hi=200)
    await port.reply(msg, await runner.clean_old_jobs(keep))


async def _maintain(msg, port, _runner, settings, arg):
    keep = _int_arg(arg, default=50, lo=1, hi=500)
    try:
        outcome = await run_maintenance(".env", "conveyor-telegram-bot", clean_threshold=100, keep=keep)
    except Exception as exc:
        await port.reply(msg, f"maintain 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, outcome.summary)


async def _doctor(msg, port, _runner, settings, _arg):
    results = [
        check_systemd_active("conveyor-telegram-bot"),
        check_workspace(settings),
        check_minimax_models(settings),
        check_disk(settings.codex_task_root),
    ]
    results.extend(check_runtime_dirs(settings))
    results.extend(check_latest_job(settings))
    lines = [r.line() for r in results]
    await port.reply(msg, "\n".join(lines))


async def _diag(msg, port, _runner, _settings, arg):
    since = _join_arg(arg) or "1 hour ago"
    try:
        text = diagnostics_report(".env", "conveyor-telegram-bot", since, metrics_limit=20)
    except Exception as exc:
        await port.reply(msg, f"diag 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, text)


async def _security(msg, port, _runner, _settings, arg):
    since = _join_arg(arg) or "1 hour ago"
    try:
        results = run_security_audit(".env", "conveyor-telegram-bot", since)
    except Exception as exc:
        await port.reply(msg, f"security 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, "\n".join(r.line() for r in results))


async def _ratelimit(msg, port, _runner, _settings, arg):
    limit = _int_arg(arg, default=5, lo=1, hi=20)
    await port.reply(msg, rate_limit_report(".env", limit))


async def _audit(msg, port, _runner, _settings, arg):
    stale = _int_arg(arg, default=90, lo=1, hi=24 * 60)
    try:
        results = run_job_audit(".env", stale)
    except Exception as exc:
        await port.reply(msg, f"audit 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, "\n".join(r.line() for r in results))


async def _log(msg, port, _runner, _settings, arg):
    selector = _join_arg(arg) or "latest"
    try:
        text = summarize_log(".env", selector, limit=12)
    except Exception as exc:
        await port.reply(msg, f"log 没读成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, text)


async def _meta(msg, port, _runner, _settings, arg):
    selector = _join_arg(arg) or "latest"
    try:
        text = metadata_report(".env", selector)
    except Exception as exc:
        await port.reply(msg, f"meta 没读成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, text)


async def _metrics(msg, port, _runner, _settings, arg):
    limit = _int_arg(arg, default=20, lo=1, hi=100)
    await port.reply(msg, metrics_report(".env", limit))


def _health_summary(snapshot: dict) -> str:
    latest = snapshot.get("latest_job") if isinstance(snapshot.get("latest_job"), dict) else {}
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    checks = snapshot.get("checks") if isinstance(snapshot.get("checks"), dict) else {}
    offline = checks.get("offline_harnesses", [])
    offline_status = " ".join(
        f"{item.get('name')}={'ok' if item.get('ok') else 'fail'}"
        for item in offline
        if isinstance(item, dict)
    ) or "none"
    triage = snapshot.get("triage") if isinstance(snapshot.get("triage"), list) else []
    failed_checks = [
        item
        for group in checks.values()
        if isinstance(group, list)
        for item in group
        if isinstance(item, dict) and not item.get("ok")
    ]
    if not snapshot.get("ok"):
        lines = ["Health: failed"]
        if failed_checks:
            lines.append("Failing checks:")
            lines.extend(f"- {item.get('name', 'check')}: {item.get('detail', '')}" for item in failed_checks[:6])
        if triage:
            lines.append("Triage:")
            lines.extend(str(item) for item in triage[:4])
        else:
            lines.append("Triage: Run /diag for details.")
        lines.append(
            f"Recent: jobs={metrics.get('count', 0)} success={metrics.get('success_rate', 0)}%"
            f" rate_limits={metrics.get('rate_limit_hits', 0)}"
        )
        return "\n".join(lines)

    lines = [
        f"Health: {'ok' if snapshot.get('ok') else 'failed'}",
        f"Latest: {latest.get('id', '(none)')} · {latest.get('state', 'unknown')} · {latest.get('summary', '')}",
        f"Recent: jobs={metrics.get('count', 0)} success={metrics.get('success_rate', 0)}%"
        f" rate_limits={metrics.get('rate_limit_hits', 0)}",
        f"Offline: {offline_status}",
    ]
    if triage:
        lines.append("Triage:")
        lines.extend(str(item) for item in triage[:4])
    else:
        lines.append("Triage: No failing checks.")
    return "\n".join(lines)


async def _health(msg, port, _runner, _settings, arg):
    raw = arg.lower().split()
    compact_json = "json" in raw
    full = "full" in raw
    try:
        snapshot = health_snapshot(
            ".env",
            "conveyor-telegram-bot",
            "1 hour ago",
            metrics_limit=20,
            include_security=full and "nosecurity" not in raw,
            include_offline=full,
        )
    except Exception as exc:
        await port.reply(msg, f"health 没跑成：{truncate(str(exc), 1200)}")
        return
    if compact_json:
        await port.reply(msg, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        return
    await port.reply(msg, _health_summary(snapshot))


async def _smoke(msg, port, _runner, _settings, _arg):
    await port.reply(msg, "开始 smoke。它会跑一条最小 MiniMax/Codex 端到端测试。")
    try:
        code = await run_smoke(".env", "conveyor-telegram-bot", notify=False)
    except Exception as exc:
        await port.reply(msg, f"smoke 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, "smoke 通过。" if code == 0 else "smoke 失败，发 /doctor 看细节。")


async def _editcheck(msg, port, _runner, _settings, _arg):
    await port.reply(msg, "开始 editcheck。它会建临时 repo，让 Codex 真改一个文件，然后自动验收和清理。")
    try:
        outcome = await run_edit_harness(".env", notify=False)
    except Exception as exc:
        await port.reply(msg, f"editcheck 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, outcome.summary)


async def _memory(msg, port, runner, _settings, arg):
    """Optional positional args: [date] [category] (any order)."""
    date_str: str | None = None
    category: str | None = None
    for raw in (arg.split() if arg else []):
        token = raw.strip()
        if not token:
            continue
        if date_str is None and DATE_ARG_PATTERN.match(token):
            date_str = token
            continue
        lowered = token.lower()
        if category is None and lowered in runner.MEMO_CATEGORIES:
            category = lowered
            continue
    if date_str is not None:
        text = runner.read_journal(date_str, category)
        if not text:
            if category:
                await port.reply(msg, f"没找到或为空：Journal {date_str} 的 ## {category} 段不存在。")
            else:
                await port.reply(msg, f"没找到或为空：Journal {date_str} 还没有。")
            return
        header = f"Journal {date_str}"
        if category:
            header += f" · {category}"
        await port.reply(msg, f"{header}\n{text}")
        return
    text = runner.read_memory(category)
    if not text:
        if category:
            await port.reply(msg, f"今天的 MEMORY.md 里没有 ## {category} 段。")
        else:
            await port.reply(msg, "今天的 MEMORY.md 还是空的。直接发「记 xxx」就能写。")
        return
    header = f"MEMORY.md @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if category:
        header += f" · {category}"
    await port.reply(msg, f"{header}\n{text}")


async def _journal(msg, port, runner, _settings, arg):
    first = arg.strip().split()[0] if arg.strip() else "10"
    limit = _int_arg(first, default=10, lo=1, hi=50)
    files = runner.list_journal(limit)
    if not files:
        await port.reply(msg, "JOURNAL/ 还没有条目。首次 12 点刷新后会出现。")
        return
    lines = [f"Journal (most recent {len(files)}):"]
    for path in files:
        size = path.stat().st_size
        lines.append(f"  {path.name}  ({size} bytes)")
    await port.reply(msg, "\n".join(lines))


# Slash aliases shown in /tools (tool name -> slash commands).
_TOOL_SLASH: dict[str, tuple[str, ...]] = {
    "load": ("/load", "/vps"),
    "ps": ("/ps",),
    "htop": ("/htop",),
    "disk": ("/disk",),
    "logs": ("/logs",),
    "service_status": ("/service_status",),
    "git_status": ("/git_status",),
    "service_restart": ("/restart telegram|feishu|maintain",),
    "scheduler_status": ("/scheduler_status",),
    "scheduler_probe": ("/scheduler_probe",),
    "scheduler_probe_live": ("/scheduler_probe_live",),
    "gmail.status": ("/gmail_status",),
    "gmail.recent": ("/gmail_recent",),
    "gmail.search": ("/gmail_search",),
    "gmail.read": ("/gmail_read",),
    "email.send": ("/email_send",),
    "google.status": ("/google_status",),
    "google.auth": ("/auth_google",),
    "google.revoke": ("/google_revoke",),
    "calendar.status": ("/calendar_status",),
    "calendar.today": ("/calendar_today",),
    "calendar.tomorrow": ("/calendar_tomorrow",),
    "calendar.week": ("/calendar_week",),
    "calendar.search": ("/calendar_search",),
    "calendar.freebusy": ("/calendar_freebusy",),
    "calendar.create": ("/calendar_create",),
    "contacts.search": ("/contacts_search",),
    "briefing.status": ("/brief_settings",),
    "briefing.today": ("/brief_today",),
    "briefing.tomorrow": ("/brief_tomorrow",),
    "briefing.enable": ("/brief_enable",),
    "briefing.disable": ("/brief_disable",),
    "briefing.probe": ("/brief_probe",),
    "github.status": ("/github_status",),
    "github.issues": ("/github_issues",),
    "github.issue": ("/github_issue",),
    "github.prs": ("/github_prs",),
    "github.pr": ("/github_pr",),
    "github.ci": ("/github_ci",),
    "github.create_issue": ("/github_create_issue",),
    "github.comment": ("/github_comment",),
    "planner.list": ("/planners",),
    "planner.today": ("/plan_today",),
    "planner.dev": ("/plan_dev",),
    "planner.health": ("/planner_health",),
    "planner.triage": ("/inbox_triage",),
    "planner.schedule": ("/schedule_review",),
    "projects.list": ("/projects",),
    "projects.add": ("/project_add",),
    "projects.use": ("/project_use",),
    "projects.show": ("/project_show",),
    "projects.remove": ("/project_remove",),
    "project.status": ("/project_status",),
    "project.health": ("/project_health",),
    "project.roadmap": ("/project_roadmap",),
    "project.next": ("/project_next",),
    "project.release_checklist": ("/project_release_checklist",),
    "project.brief": ("/project_brief",),
    "setup.status": ("/setup", "/setup_status"),
    "setup.check": ("/setup_check",),
    "setup.project": ("/setup_project",),
    "setup.gmail": ("/setup_gmail",),
    "setup.google": ("/setup_google",),
    "setup.github": ("/setup_github",),
    "project.export": ("/project_export",),
    "project.export_all": ("/project_export_all",),
    "project.import": ("/project_import",),
    "project.template": ("/project_template",),
    # Web / Research (P4.1)
    "web.fetch": ("/web_fetch",),
    "web.text": ("/web_text",),
    "web.headers": ("/web_headers",),
    "web.search": ("/web_search",),
    "research.run": ("/research",),
    "research.project": ("/project_research",),
    # File Search / Knowledge Base (P4.2)
    "files.list_roots": ("/files_roots",),
    "files.search": ("/files_search",),
    "files.read": ("/files_read",),
    "kb.index": ("/kb_index",),
    "kb.status": ("/kb_status",),
    "kb.search": ("/kb_search",),
    "kb.collect_facts": ("/kb_collect_facts",),
    # Execution nodes (P5.0)
    "nodes.status": ("/nodes", "/node_status"),
    "computer.status": ("/computer_status",),
    "desktop.screenshot.status": ("/desktop_screenshot_status", "/screenshot_status"),
    "desktop.observe.request": ("/observe_request", "/screenshot_request", "/request_screenshot"),
    "desktop.observe.status": ("/observe_status",),
    "desktop.observe.cancel": ("/observe_cancel",),
    "desktop.upload.request": ("/observe_upload", "/screenshot_upload"),
    "desktop.upload.status": ("/upload_status",),
    "desktop.upload.cancel": ("/upload_cancel",),
    "desktop.upload.cleanup": ("/upload_cleanup",),
}

_TOOL_EXAMPLES: dict[str, str] = {
    "load": "看看负载",
    "disk": "磁盘空间",
    "logs": "看日志",
    "service_status": "服务还在跑吗",
    "git_status": "git status",
    "service_restart": "重启 telegram bot",
    "scheduler_status": "调度器状态",
    "scheduler_probe": "探测调度器",
    "scheduler_probe_live": "实时测试投递",
    "gmail.status": "邮箱状态",
    "gmail.recent": "最近邮件",
    "gmail.search": "搜索邮件",
    "gmail.read": "读取邮件",
    "email.send": "发邮件",
    "google.status": "google 状态",
    "google.auth": "授权 google",
    "calendar.status": "日历状态",
    "calendar.today": "今天的日程",
    "calendar.tomorrow": "明天的日程",
    "calendar.week": "本周日程",
    "calendar.search": "搜索日程",
    "calendar.freebusy": "查询忙闲",
    "calendar.create": "创建日程",
    "contacts.search": "搜索联系人",
    "briefing.status": "简报设置",
    "briefing.today": "今日简报",
    "briefing.tomorrow": "明日简报",
    "briefing.enable": "启用简报",
    "briefing.disable": "禁用简报",
    "briefing.probe": "简报探针",
    "github.status": "github 状态",
    "github.issues": "看看 issue",
    "github.issue": "查看 issue",
    "github.prs": "看看 PR",
    "github.pr": "查看 PR",
    "github.ci": "CI 状态",
    "github.create_issue": "创建 issue",
    "github.comment": "评论",
    "planner.list": "看看 planner",
    "planner.today": "今天应该先干啥",
    "planner.dev": "今天开发计划",
    "planner.health": "项目健康状态",
    "planner.triage": "帮我整理邮件",
    "planner.schedule": "今天日程安排",
    "projects.list": "项目列表",
    "projects.add": "添加项目",
    "projects.use": "切换项目",
    "projects.show": "项目详情",
    "projects.remove": "删除项目",
    "project.status": "项目状态",
    "project.health": "项目健康",
    "project.roadmap": "项目 roadmap",
    "project.next": "项目下一步",
    "project.release_checklist": "发布清单",
    "project.brief": "项目简报",
    "setup.status": "配置状态",
    "setup.check": "检查清单",
    "setup.project": "项目配置",
    "setup.gmail": "gmail 配置",
    "setup.google": "google 配置",
    "setup.github": "github 配置",
    "project.export": "导出项目",
    "project.export_all": "导出所有项目",
    "project.import": "导入项目",
    "project.template": "项目模板",
    # Web / Research (P4.1)
    "web.fetch": "获取网页",
    "web.text": "网页文本",
    "web.headers": "HTTP headers",
    "web.search": "搜索",
    "research.run": "研究",
    "research.project": "项目研究",
    # File Search / Knowledge Base (P4.2)
    "files.list_roots": "搜索根目录",
    "files.search": "搜索文件",
    "files.read": "读取文件",
    "kb.index": "索引知识库",
    "kb.status": "知识库状态",
    "kb.search": "知识库搜索",
    "kb.collect_facts": "收集文档证据",
    # Execution nodes (P5.0)
    "nodes.status": "节点状态",
    "computer.status": "Computer Use 状态",
    "desktop.screenshot.status": "桌面截图 observe 状态",
    "screenshot_status": "桌面截图状态",
    "desktop.observe.request": "创建远程截图 observe 请求",
    "desktop.observe.status": "observe 请求状态",
    "desktop.observe.cancel": "取消 observe 请求",
}


async def _diagnose(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_diagnose_command
    await handle_diagnose_command(msg, port, runner, settings, arg)


async def _restart(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    unit = resolve_restart_alias(arg)
    if unit is None:
        await port.reply(msg, RESTART_USAGE)
        return
    await _invoke_tool(msg, port, settings, "service_restart", unit)


async def _note(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "notes.add", arg)


async def _notes(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    query = (arg or "").strip()
    if not query:
        await _invoke_tool(msg, port, settings, "notes.list_recent", "")
        return
    await _invoke_tool(msg, port, settings, "notes.search", query)


async def _remind(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "reminders.create", arg)


async def _reminders(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "reminders.list", arg)


async def _gmail_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "gmail.status"))


async def _gmail_recent(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "gmail.recent", arg))


async def _gmail_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /gmail_search <关键词>")
        return
    await port.reply(msg, await run_tool(settings, "gmail.search", arg))


async def _gmail_read(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /gmail_read <邮件ID>")
        return
    await port.reply(msg, await run_tool(settings, "gmail.read", arg))


async def _email_send(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /email_send <收件人> | <主题> | <正文>")
        return
    await _invoke_tool(msg, port, settings, "email.send", arg)


# Google OAuth / Calendar / Contacts commands (P3.4)

async def _google_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "google.status"))


async def _auth_google(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "google.auth", arg)


async def _google_revoke(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "google.revoke", "")


async def _calendar_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "calendar.status"))


async def _calendar_today(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "calendar.today"))


async def _calendar_tomorrow(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "calendar.tomorrow"))


async def _calendar_week(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "calendar.week"))


async def _calendar_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /calendar_search <关键词>")
        return
    await port.reply(msg, await run_tool(settings, "calendar.search", arg))


async def _calendar_freebusy(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /calendar_freebusy <时间范围>\n示例: /calendar_freebusy 14:00-16:00")
        return
    await port.reply(msg, await run_tool(settings, "calendar.freebusy", arg))


async def _calendar_create(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /calendar_create <标题> | <时间> | <描述>\n示例: /calendar_create 周会 | 明天 14:00-15:00 | 讨论计划")
        return
    await _invoke_tool(msg, port, settings, "calendar.create", arg)


async def _contacts_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /contacts_search <关键词>")
        return
    await port.reply(msg, await run_tool(settings, "contacts.search", arg))


# GitHub commands (P3.6)

async def _github_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "github.status"))


async def _github_issues(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    query = arg.strip() if arg.strip() else "open"
    await port.reply(msg, await run_tool(settings, "github.issues", query))


async def _github_issue(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /github_issue <number>")
        return
    await port.reply(msg, await run_tool(settings, "github.issue", arg.strip()))


async def _github_prs(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    state = arg.strip() if arg.strip() else "open"
    await port.reply(msg, await run_tool(settings, "github.prs", state))


async def _github_pr(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /github_pr <number>")
        return
    await port.reply(msg, await run_tool(settings, "github.pr", arg.strip()))


async def _github_ci(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "github.ci", arg.strip()))


async def _github_create_issue(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /github_create_issue <标题> | <正文>")
        return
    await _invoke_tool(msg, port, settings, "github.create_issue", arg)


async def _github_comment(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /github_comment <number> | <正文>")
        return
    await _invoke_tool(msg, port, settings, "github.comment", arg)


# Daily Briefing commands (P3.5)

async def _brief_today(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "briefing.today"))


async def _brief_tomorrow(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "briefing.tomorrow"))


async def _brief_settings(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "briefing.status"))


async def _brief_enable(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    local_time = arg.strip() if arg.strip() else "09:00"
    await _invoke_tool(msg, port, settings, "briefing.enable", local_time)


async def _brief_disable(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "briefing.disable", "")


async def _brief_probe(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "briefing.probe"))


# Planner commands (P3.7)

async def _plan_today(msg, port, runner, settings, _arg):
    from handlers.tools.runner import handle_hybrid
    from handlers.intent import RouteResult
    from personal_tools.planner import DAILY_PRIORITY, build_planner_prompt
    route = RouteResult(kind="hybrid", tool_items=DAILY_PRIORITY.tool_items, question="今日优先级分析")
    await handle_hybrid(msg, port, runner, settings, route)


async def _plan_dev(msg, port, runner, settings, _arg):
    from handlers.tools.runner import handle_hybrid
    from handlers.intent import RouteResult
    from personal_tools.planner import DEV_PLAN
    route = RouteResult(kind="hybrid", tool_items=DEV_PLAN.tool_items, question="开发计划")
    await handle_hybrid(msg, port, runner, settings, route)


async def _plan_health(msg, port, runner, settings, _arg):
    from handlers.tools.runner import handle_hybrid
    from handlers.intent import RouteResult
    from personal_tools.planner import PROJECT_HEALTH
    route = RouteResult(kind="hybrid", tool_items=PROJECT_HEALTH.tool_items, question="项目健康检查")
    await handle_hybrid(msg, port, runner, settings, route)


async def _plan_triage(msg, port, runner, settings, _arg):
    from handlers.tools.runner import handle_hybrid
    from handlers.intent import RouteResult
    from personal_tools.planner import INBOX_TRIAGE
    route = RouteResult(kind="hybrid", tool_items=INBOX_TRIAGE.tool_items, question="邮件分类整理")
    await handle_hybrid(msg, port, runner, settings, route)


async def _plan_schedule(msg, port, runner, settings, _arg):
    from handlers.tools.runner import handle_hybrid
    from handlers.intent import RouteResult
    from personal_tools.planner import SCHEDULE_REVIEW
    route = RouteResult(kind="hybrid", tool_items=SCHEDULE_REVIEW.tool_items, question="日程审查")
    await handle_hybrid(msg, port, runner, settings, route)


async def _planners(msg, port, _runner, _settings, _arg):
    from personal_tools.planner import planner_status
    result = planner_status()
    await port.reply(msg, result.text)


# Project commands (P3.9)

async def _projects(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "projects.list"))


async def _project_add(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, (
            "用法: /project_add <名称> | <类型> | <描述> | [github_repo] | [关键词]\n\n"
            "示例:\n"
            "  /project_add My App | mobile_app | iOS 待办应用 | user/repo | todo,productivity\n"
            "  /project_add 研究课题 | research | AI 对 NLP 的影响\n\n"
            "支持的类型: generic, mobile_app, web_app, bot, library, research, course, business"
        ))
        return
    await _invoke_tool(msg, port, settings, "projects.add", arg)


async def _project_use(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /project_use <id>")
        return
    await _invoke_tool(msg, port, settings, "projects.use", arg)


async def _project_show(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "projects.show", arg))


async def _project_remove(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /project_remove <id>")
        return
    await _invoke_tool(msg, port, settings, "projects.remove", arg)


async def _project_status(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.status", arg)


async def _project_health(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.health", arg)


async def _project_roadmap(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.roadmap", arg)


async def _project_next(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.next", arg)


async def _project_release_checklist(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.release_checklist", arg)


async def _project_brief(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    await handle_hybrid_project(msg, port, runner, settings, "project.brief", arg)


# Setup commands (P3.10)

async def _setup(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.status"))


async def _setup_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.status"))


async def _setup_check(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.check"))


async def _setup_project(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.project"))


async def _setup_gmail(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.gmail"))


async def _setup_google(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.google"))


async def _setup_github(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "setup.github"))


# Project Import/Export (P3.11)

async def _project_export(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "project.export", arg))


async def _project_export_all(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "project.export_all"))


async def _project_import(msg, port, _runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    if not arg.strip():
        await port.reply(msg, "用法: /project_import <JSON>")
        return
    await _invoke_tool(msg, port, settings, "project.import", arg)


async def _project_template(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "project.template", arg))


# File Search / Knowledge Base (P4.2)

async def _files_roots(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "files.list_roots"))


async def _files_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /files_search <查询词>")
        return
    await port.reply(msg, await run_tool(settings, "files.search", arg))


async def _files_read(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /files_read <文件路径>")
        return
    await port.reply(msg, await run_tool(settings, "files.read", arg))


async def _kb_index(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "kb.index", "")


async def _kb_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "kb.status"))


async def _kb_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /kb_search <查询词>")
        return
    await port.reply(msg, await run_tool(settings, "kb.search", arg))


async def _project_docs(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    if not arg.strip():
        await port.reply(msg, "用法: /project_docs <查询词>")
        return
    await handle_hybrid_project(msg, port, runner, settings, "kb.collect_facts", arg)


async def _kb_collect_facts(msg, port, runner, settings, arg):
    from handlers.tools.runner import handle_hybrid_project
    if not arg.strip():
        await port.reply(msg, "用法: /kb_collect_facts <查询词>")
        return
    await handle_hybrid_project(msg, port, runner, settings, "kb.collect_facts", arg)


# Web Fetch / Search / Research (P4.1)

async def _web_fetch(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /web_fetch <url>")
        return
    await port.reply(msg, await run_tool(settings, "web.fetch", arg))


async def _web_text(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /web_text <url>")
        return
    await port.reply(msg, await run_tool(settings, "web.text", arg))


async def _web_headers(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /web_headers <url>")
        return
    await port.reply(msg, await run_tool(settings, "web.headers", arg))


async def _web_search(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    if not arg.strip():
        await port.reply(msg, "用法: /web_search <查询词>")
        return
    await port.reply(msg, await run_tool(settings, "web.search", arg))


async def _research(msg, port, runner, settings, arg):
    from handlers.tools.runner import run_tool, handle_hybrid_project
    if not arg.strip():
        await port.reply(msg, "用法: /research <问题>")
        return
    # Use hybrid synthesis for research
    await handle_hybrid_project(msg, port, runner, settings, "research.run", arg)


async def _project_research(msg, port, runner, settings, arg):
    from handlers.tools.runner import run_tool, handle_hybrid_project
    if not arg.strip():
        await port.reply(msg, "用法: /project_research [项目ID] <问题>")
        return
    # Use hybrid synthesis for project research
    await handle_hybrid_project(msg, port, runner, settings, "research.project", arg)


async def _scheduler_status(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "scheduler_status"))


async def _scheduler_probe(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "scheduler_probe"))


async def _scheduler_probe_live(msg, port, _runner, settings, _arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(msg, port, settings, "scheduler_probe_live", "")


async def _audit_tools(msg, port, _runner, settings, arg):
    from handlers.tools.audit import read_audit_tail
    from redaction import redact_text, truncate
    n = _int_arg(arg.strip().split()[0] if arg.strip() else "", default=10, lo=1, hi=50)
    records = read_audit_tail(settings, n)
    if not records:
        await port.reply(msg, "暂无工具审计日志。")
        return
    lines = [f"工具审计 (最近 {len(records)} 条):", ""]
    for rec in records:
        ts = rec.get("timestamp", "?")
        action = rec.get("action", "?")
        tool = rec.get("tool_name", "?")
        danger = rec.get("danger", "?")
        arg_preview = rec.get("arg", "")
        line = f"  {ts} | {action} | {tool} ({danger})"
        if arg_preview:
            line += f" arg={arg_preview}"
        lines.append(truncate(redact_text(line), 500))
    lines.append("\n路径: audit/tools.log（已 redact）")
    await port.reply(msg, truncate("\n".join(lines)))


async def _deploy_status(msg, port, _runner, settings, _arg):
    """Show last deploy status from .deploy-status.json + live service info."""
    import subprocess
    import shutil
    from pathlib import Path

    status_file = Path(".deploy-status.json")
    lines = ["📦 部署状态", ""]

    # Read .deploy-status.json
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            lines.append(f"部署时间: {data.get('deployed_at', '—')}")
            lines.append(f"来源: {data.get('source', '—')}")
            lines.append(f"Git SHA: {data.get('git_sha', '—')}")
            ref = data.get("git_ref", "")
            if ref:
                lines.append(f"Git Ref: {ref}")
            run_id = data.get("run_id", "")
            if run_id:
                lines.append(f"Run ID: {run_id}")
            lines.append(f"Smoke: {data.get('smoke', '—')}")
            svc = data.get("services", {})
            lines.append(f"Telegram: {svc.get('telegram', '—')}")
            lines.append(f"Feishu: {svc.get('feishu', '—')}")
            if data.get("rollback_attempted"):
                lines.append("⚠️ 上次部署触发了回滚")
        except (json.JSONDecodeError, OSError) as exc:
            lines.append(f"状态文件读取失败: {exc}")
    else:
        lines.append("暂无部署状态记录")

    # Live runtime info
    lines.append("")
    lines.append("── 当前运行时 ──")

    # Git info
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        lines.append(f"当前 Git SHA: {sha}")
    except Exception:
        lines.append("当前 Git SHA: (unknown)")

    try:
        ref = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        lines.append(f"当前分支: {ref}")
    except Exception:
        pass

    # Progress mode
    lines.append(f"Progress mode: {settings.conveyor_progress_mode}")

    # Live service status (safe, read-only)
    if shutil.which("systemctl"):
        for svc_name in ("conveyor-telegram-bot", "conveyor-feishu-bot"):
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc_name],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=5, check=False,
                )
                state = (result.stdout or "").strip() or "unknown"
            except Exception:
                state = "(timeout)"
            lines.append(f"{svc_name}: {state}")
    else:
        lines.append("systemctl 不可用，服务状态未知")

    await port.reply(msg, "\n".join(lines))


async def _deploy_verify(msg, port, _runner, settings, _arg):
    """P5.2 deployment-readiness check for desktop screenshot observe."""
    import subprocess
    from desktop_screenshot import (
        helper_configuration_error,
        latest_screenshot_metadata,
        resolve_helper_path,
        resolve_screenshot_dir,
    )
    from handlers.tools.executors import exec_desktop_screenshot_status
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    lines = ["P5.2/P5.3 Deploy Verify", ""]

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        lines.append(f"Git SHA: {sha}")
    except Exception:
        lines.append("Git SHA: (unknown)")

    helper_error = helper_configuration_error(settings)
    if helper_error == "screenshot_helper_not_configured":
        lines.append("Helper: not configured")
    elif helper_error == "screenshot_helper_path_not_absolute":
        lines.append("Helper: configured but path is not absolute")
    else:
        helper = resolve_helper_path(settings)
        lines.append(f"Helper: configured ({helper})")

    screenshot_dir = resolve_screenshot_dir(settings)
    lines.append(
        f"Screenshot dir: {'exists' if screenshot_dir.is_dir() else 'missing'} "
        f"({screenshot_dir})"
    )

    from handlers.tools.observe_tools import upload_temp_dir_configuration_error, resolve_upload_temp_dir
    temp_dir_err = upload_temp_dir_configuration_error(settings)
    upload_dir = resolve_upload_temp_dir(settings)
    if temp_dir_err:
        lines.append(f"Upload temp dir: invalid ({temp_dir_err})")
    else:
        lines.append(
            f"Upload temp dir: {'exists' if upload_dir.is_dir() else 'missing'} "
            f"({upload_dir})"
        )

    latest = latest_screenshot_metadata(settings)
    if latest:
        lines.append(f"Latest metadata: {latest.get('screenshot_id', '?')}")
        if latest.get("created_at"):
            lines.append(f"  created: {latest['created_at']}")
        if latest.get("bytes") is not None:
            lines.append(f"  bytes: {latest['bytes']}")
    else:
        lines.append("Latest metadata: (none)")

    desktop_nodes = [n for n in list_nodes(settings) if n.node_type == NodeType.DESKTOP]
    if desktop_nodes:
        node = desktop_nodes[0]
        state = "online" if node.status == NodeStatus.ONLINE else "offline"
        lines.append(f"Desktop node: {state} ({node.node_id})")
    else:
        lines.append("Desktop node: not enabled")

    from desktop_observe_requests import observe_requests_path

    observe_path = observe_requests_path(settings)
    lines.append(f"Observe request store: {observe_path}")

    lines.extend([
        "",
        "This command does not capture a screenshot.",
        "Local one-shot: python desktop_agent.py --observe-once",
        "Remote observe: /observe_request (chat) + python desktop_agent.py --poll-observe (Mac)",
        "",
        "── Screenshot / observe status detail ──",
    ])

    status_text = await exec_desktop_screenshot_status(settings, "")
    lines.append(status_text)
    await port.reply(msg, truncate("\n".join(lines), 3500))


async def _tool_disk(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "disk", arg))


# ---- P5.0: Execution nodes (VPS + desktop stub) -------------------------

async def _nodes(msg, port, _runner, settings, _arg):
    """List known execution nodes (VPS + optional desktop stub)."""
    from handlers.tools.runner import run_tool
    text = await run_tool(settings, "nodes.status", "")
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        try:
            from channel.feishu_cards import node_status_card
            await port.send_card(msg, node_status_card(text))
        except Exception:
            pass
    await port.reply(msg, text)


async def _node_status(msg, port, _runner, settings, _arg):
    """Alias of /nodes. Same text + same Feishu card."""
    await _nodes(msg, port, _runner, settings, _arg)


async def _computer_status(msg, port, _runner, settings, _arg):
    """Stub status for Computer Use (desktop agent) requests.

    Real desktop control is not implemented in this task — see
    ``docs/desktop_security.md``. The slash command exists so the
    operator can probe the layer without triggering a Codex job.
    """
    from handlers.tools.runner import run_tool
    text = await run_tool(settings, "computer.status", _arg or "")
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        try:
            from channel.feishu_cards import computer_status_card
            await port.send_card(msg, computer_status_card(text))
        except Exception:
            pass
    await port.reply(msg, text)


async def _desktop_screenshot_status(msg, port, _runner, settings, _arg):
    """Read-only desktop screenshot observe status (P5.2/P5.3)."""
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.screenshot.status", _arg or "", runner=_runner,
    )


async def _observe_request(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.observe.request", arg or msg.text.strip(), runner=runner,
    )


async def _observe_status(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.observe.status", arg or "", runner=runner,
    )


async def _observe_cancel(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.observe.cancel", arg or "", runner=runner,
    )


async def _observe_upload(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.upload.request", arg or "", runner=runner,
    )


async def _screenshot_upload(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.upload.request", arg or "", runner=runner,
    )


async def _upload_status(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.upload.status", arg or "", runner=runner,
    )


async def _upload_cancel(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.upload.cancel", arg or "", runner=runner,
    )


async def _upload_cleanup(msg, port, runner, settings, arg):
    from handlers.tools.runner import _invoke_tool
    await _invoke_tool(
        msg, port, settings, "desktop.upload.cleanup", arg or "", runner=runner,
    )



async def _tool_logs(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "logs", arg))


async def _tool_service_status(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "service_status", arg))


async def _tool_git_status(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "git_status", arg))


async def _tools(msg, port, _runner, _settings, _arg):
    lines = [
        "Agent 工具层",
        "",
        "Hybrid 诊断:",
        "  /diagnose [server|bot|logs|quick] — 采集事实 + Codex 分析",
        "  自然语言: 「为什么服务器慢」「诊断服务器」",
        "",
        "危险操作别名:",
        "  /restart telegram|feishu|maintain — 映射 systemd 单元，需确认",
        "",
    ]
    by_danger: dict[DangerLevel, list[str]] = {
        DangerLevel.READ: [],
        DangerLevel.WRITE_SAFE: [],
        DangerLevel.WRITE: [],
        DangerLevel.DESTRUCTIVE: [],
    }
    for name in sorted(TOOL_REGISTRY):
        spec = TOOL_REGISTRY[name]
        slashes = " ".join(_TOOL_SLASH.get(name, (f"/{name}",)))
        example = _TOOL_EXAMPLES.get(name, "")
        ex_part = f' · 例: "{example}"' if example else ""
        entry = f"  {slashes} — {name}: {spec.summary}{ex_part}"
        by_danger[spec.danger].append(entry)

    lines.append("READ (立即执行):")
    lines.extend(by_danger[DangerLevel.READ] or ["  (none)"])
    lines.append("")
    write_safe_entries = by_danger[DangerLevel.WRITE_SAFE]
    if write_safe_entries:
        lines.append("WRITE_SAFE (立即执行, 审计):")
        lines.extend(write_safe_entries)
        lines.append("")
    write_entries = by_danger[DangerLevel.WRITE]
    if write_entries:
        lines.append("WRITE (需确认):")
        lines.extend(write_entries)
        lines.append("")
    destruct_entries = by_danger[DangerLevel.DESTRUCTIVE]
    if destruct_entries:
        lines.append("DESTRUCTIVE (需确认):")
        lines.extend(destruct_entries)
        lines.append("")

    from personal_tools.registry import PERSONAL_TOOL_REGISTRY
    if PERSONAL_TOOL_REGISTRY:
        lines.append("Personal Tools (本地):")
        lines.append("  /note /notes /remind /reminders")
        lines.append("  /gmail_status /gmail_recent /gmail_search /gmail_read /email_send")
        lines.append("  /auth_google /google_status /calendar_today /calendar_search /contacts_search")
        lines.append("  /brief_today /brief_tomorrow /brief_settings /brief_enable /brief_disable /brief_probe")
        lines.append("  /github_status /github_issues /github_prs /github_ci /github_create_issue /github_comment")
        lines.append("  /plan_today /plan_dev /planner_health /inbox_triage /schedule_review /planners")
        for name in sorted(PERSONAL_TOOL_REGISTRY):
            pspec = PERSONAL_TOOL_REGISTRY[name]
            lines.append(f"  {name} ({pspec.danger.value}): {pspec.summary}")
        lines.append("")

    lines += [
        "确认规则:",
        "  Telegram: 点内联按钮 ✅ 确认",
        "  文本 fallback: 回复「确认执行」",
        "  取消: 「取消」「算了」「no」",
        "  随意的「好/ok/是/y」不会确认（防误触）",
        "",
        "也可: tool load / tool ps",
    ]
    await port.reply(msg, "\n".join(lines))


async def _context(msg, port, _runner, settings, _arg):
    """Show the compact recent session summary for this chat/operator."""
    from handlers.session import get_recent_turns
    turns = get_recent_turns(settings, msg)
    if not turns:
        await port.reply(msg, "暂无会话记录。")
        return
    lines = [f"最近 {len(turns)} 条对话记录：", ""]
    for t in turns:
        user = t.get("user", "")
        assistant = t.get("assistant", "")
        if user:
            lines.append(f"用户: {user}")
        if assistant:
            lines.append(f"助手: {assistant}")
        lines.append("")
    await port.reply(msg, "\n".join(lines))


async def _forget(msg, port, _runner, settings, _arg):
    """Clear this chat/operator session."""
    from handlers.session import clear_session
    removed = clear_session(settings, msg)
    if removed:
        await port.reply(msg, "会话记录已清除。")
    else:
        await port.reply(msg, "没有需要清除的会话记录。")


async def _help(msg, port, _runner, _settings, _arg):
    text = "Codex Bot\n"
    text += "直接发文字 → 跑 Codex（danger-full-access）\n"
    text += "记 xxx / /memo xxx → 写 MEMORY.md（不经 Codex）\n"
    text += "/status /last /diff /apply /discard /cancel\n"
    text += "/jobs [n] /memory [date] [cat] /journal [n]\n"
    text += "/health [full] [json] [nosecurity] /doctor /diag [since] /audit [stale-min]\n"
    text += "/security [since] /ratelimit [n] /metrics [n] /log [sel] /meta [sel]\n"
    text += "/smoke /editcheck /maintain [keep] /clean [keep] /run /fix\n"
    text += "/note <内容> /notes [关键词] /remind <内容+时间> /reminders\n"
    text += "/scheduler_status /scheduler_probe /scheduler_probe_live\n"
    text += "\n"
    text += "Gmail 邮件 (需要配置 GMAIL_BACKEND):\n"
    text += "/gmail_status — 连接状态\n"
    text += "/gmail_recent [n] — 最近邮件\n"
    text += "/gmail_search <关键词> — 搜索邮件\n"
    text += "/gmail_read <邮件ID> — 读取邮件\n"
    text += "/email_send <收件人> | <主题> | <正文> — 发送邮件 (需确认)\n"
    text += "\n"
    text += "Google 日历/联系人 (需要配置 OAuth):\n"
    text += "/auth_google — 授权 Google OAuth\n"
    text += "/google_status — OAuth 状态\n"
    text += "/calendar_today /calendar_tomorrow /calendar_week — 日程\n"
    text += "/calendar_search <关键词> — 搜索日程\n"
    text += "/calendar_create <标题> | <时间> | <描述> — 创建日程 (需确认)\n"
    text += "/contacts_search <关键词> — 搜索联系人\n"
    text += "\n"
    text += "Daily Briefing (每日简报):\n"
    text += "/brief_today — 今日简报\n"
    text += "/brief_tomorrow — 明日简报\n"
    text += "/brief_settings — 简报设置状态\n"
    text += "/brief_enable [HH:MM] — 启用每日简报\n"
    text += "/brief_disable — 禁用每日简报 (需确认)\n"
    text += "/brief_probe — 简报探针 (dry-run)\n"
    text += "\n"
    text += "GitHub (需要配置 GITHUB_TOKEN):\n"
    text += "/github_status — 连接状态\n"
    text += "/github_issues [open|closed|all|query] — 列出 Issues\n"
    text += "/github_issue <number> — 查看 Issue 详情\n"
    text += "/github_prs [open|closed|all] — 列出 PRs\n"
    text += "/github_pr <number> — 查看 PR 详情\n"
    text += "/github_ci [ref] — CI 状态\n"
    text += "/github_create_issue <标题> | <正文> — 创建 Issue (审计)\n"
    text += "/github_comment <number> | <正文> — 评论 (需确认)\n"
    text += "\n"
    text += "Planner (智能规划, 需要 Codex 分析):\n"
    text += "/plan_today — 今日优先级分析\n"
    text += "/plan_dev — 开发计划\n"
    text += "/planner_health — Planner 健康检查\n"
    text += "/inbox_triage — 邮件分类整理\n"
    text += "/schedule_review — 日程审查\n"
    text += "/planners — 列出所有 Planner\n"
    text += "\n"
    text += "Project Profiles (项目管理, P3.9):\n"
    text += "/projects — 列出项目\n"
    text += "/project_add <名称> | <类型> | <描述> — 添加项目\n"
    text += "/project_use <id> — 切换活跃项目\n"
    text += "/project_show [id] — 查看项目详情\n"
    text += "/project_remove <id> — 删除项目 (需确认)\n"
    text += "/project_status [id] — 项目状态分析\n"
    text += "/project_health [id] — 项目健康检查\n"
    text += "/project_roadmap [id] — 项目 Roadmap\n"
    text += "/project_next [id] — 项目下一步行动\n"
    text += "/project_release_checklist [id] — 发布清单\n"
    text += "/project_brief [id] — 项目简报\n"
    text += "\n"
    text += "设置向导 (P3.10):\n"
    text += "/setup — 配置状态概览\n"
    text += "/setup_check — 设置检查清单\n"
    text += "/setup_project — 项目配置指南\n"
    text += "/setup_gmail — Gmail 配置指南\n"
    text += "/setup_google — Google OAuth 配置指南\n"
    text += "/setup_github — GitHub 配置指南\n"
    text += "\n"
    text += "项目导入/导出 (P3.11):\n"
    text += "/project_export [id] — 导出项目为 JSON\n"
    text += "/project_export_all — 导出所有项目\n"
    text += "/project_import <JSON> — 从 JSON 导入项目\n"
    text += "/project_template [type] — 查看项目模板\n"
    text += "\n"
    text += "File Search / Knowledge Base (P4.2):\n"
    text += "/files_roots — 列出搜索根目录\n"
    text += "/files_search <查询词> — 搜索文件\n"
    text += "/files_read <文件路径> — 读取文件\n"
    text += "/kb_index — 索引知识库\n"
    text += "/kb_status — 知识库状态\n"
    text += "/kb_search <查询词> — 搜索知识库\n"
    text += "/kb_collect_facts <查询词> — 收集本地文档证据 (KB优先)\n"
    text += "/project_docs <查询词> — 搜索项目文档\n"
    text += "自然语言: 「找一下文档里关于 deploy 的说明」「README 里有没有 Gmail 配置」\n"
    text += "\n"
    text += "Web / Research (P4.1):\n"
    text += "/web_fetch <url> — 获取网页内容\n"
    text += "/web_text <url> — 获取网页文本\n"
    text += "/web_headers <url> — 获取 HTTP headers\n"
    text += "/web_search <查询> — Web 搜索\n"
    text += "/research <问题> — Web 研究\n"
    text += "/project_research [id] <问题> — 项目研究\n"
    text += "\n"
    text += "本机运维快路径 (bypass Codex):\n"
    text += "/load /vps — 主机负载/内存/磁盘快照\n"
    text += "/htop — top 风格的进程帧 (htop 是 TUI)\n"
    text += "/ps [full confirm] — 进程快照，comm 默认；full confirm 才含 args\n"
    text += "自然语言 '看看我的负载' / '跑 htop 看看' / 'check vps load' 也走快路径。\n"
    text += "/tools — 列出 agent 工具层全部工具\n"
    text += "/diagnose [server|bot|logs|quick] — hybrid 主机诊断\n"
    text += "/restart telegram|feishu|maintain — 重启服务 (需确认)\n"
    text += "/audit_tools [n] — 查看危险工具审计日志\n"
    text += "/deploy_status — 查看最近部署状态\n"
    text += "/deploy_verify — P5.2 桌面截图 observe 部署就绪检查（不截屏）\n"
    text += "/context — 查看最近会话上下文\n"
    text += "/forget — 清除当前会话记录\n"
    text += "\n"
    text += "执行节点 (P5.0 phase 0 foundation):\n"
    text += "/nodes /node_status — VPS + 可选 desktop stub 状态\n"
    text += "/computer_status — Computer Use stub 状态\n"
    text += "/desktop_screenshot_status /screenshot_status — 截图元数据/状态（不截屏）\n"
    text += "/observe_request /screenshot_request — 创建远程 observe 请求（P5.3，仅元数据）\n"
    text += "/observe_status — 最近 observe 请求与截图元数据\n"
    text += "/observe_cancel <id> — 取消 pending/claimed 请求\n"
    text += "/observe_upload <id> — 申请手动上传截图的缩略图/预览 (P5.4)\n"
    text += "/screenshot_upload <id> — 申请手动上传指定截图的缩略图/预览 (P5.4)\n"
    text += "/upload_status — 最近上传请求与状态列表 (P5.4)\n"
    text += "/upload_cancel <id> — 取消 pending/claimed 上传请求 (P5.4)\n"
    text += "/upload_cleanup — 清理 VPS 上的临时上传文件 (P5.4)\n"
    text += "自然语言: '我的节点' / '机器状态' / 'MacBook 在线吗' / 'computer use status'\n"
    text += "本地只读截图 observe 与手动缩略图上传已支持；鼠标、键盘、浏览器控制仍是未来工作。\n"
    text += "\n"
    text += "任务队列 (P3.8):\n"
    text += "/queue — 查看队列状态\n"
    text += "/queue_cancel <id> — 取消队列任务\n"
    text += "/queue_clear — 清空队列 (需确认)\n"
    text += "/queue_pause — 暂停队列自动出队\n"
    text += "/queue_resume — 恢复队列自动出队\n"
    text += "\n"
    text += "NL-first: 直接用自然语言描述需求即可，/nl_help 查看自然语言示例。"
    await port.reply(msg, text)


async def _nl_help(msg, port, _runner, _settings, _arg):
    """Show natural language command examples."""
    from handlers.nl_router import build_nl_help
    await port.reply(msg, build_nl_help())


# P3.8: Queue commands

async def _queue_status(msg, port, _runner, _settings, _arg):
    """Show queue status."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    status = await queue.get_queue_status()
    await port.reply(msg, status)


async def _queue_cancel(msg, port, _runner, _settings, arg):
    """Cancel a queued job by ID."""
    if not arg.strip():
        await port.reply(msg, "用法: /queue_cancel <队列ID>")
        return
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    success, message = await queue.cancel(arg.strip())
    await port.reply(msg, message)


async def _queue_clear(msg, port, _runner, _settings, _arg):
    """Clear all queued jobs (requires confirmation)."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    count = queue.queue_length
    if count == 0:
        await port.reply(msg, "队列为空，无需清空。")
        return
    # For now, clear directly (WRITE operation)
    cleared = await queue.clear()
    await port.reply(msg, f"已清空队列 ({cleared} 个任务)")


async def _queue_pause(msg, port, _runner, _settings, _arg):
    """Pause automatic dequeue."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    await queue.pause()
    await port.reply(msg, "队列已暂停，新任务仍可入队但不会自动执行。")


async def _queue_resume(msg, port, _runner, _settings, _arg):
    """Resume automatic dequeue."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    await queue.resume()
    await port.reply(msg, "队列已恢复，将自动执行下一个任务。")
    # Try to start next job if any
    await queue.on_job_completed()


COMMAND_TABLE: dict[str, CommandSpec] = {
    spec.name: spec
    for spec in [
        # Telegram + Feishu
        CommandSpec("status", "当前任务", _status),
        CommandSpec("last", "最近结果", _last),
        CommandSpec("cancel", "中止任务", _cancel),
        CommandSpec("diff", "看最近改动", _diff),
        CommandSpec("apply", "应用最近改动", _apply),
        CommandSpec("discard", "丢弃最近 worktree", _discard),
        CommandSpec("jobs", "最近任务", _jobs, takes_optional_arg=True),
        CommandSpec("memory", "看今天 MEMORY.md", _memory, takes_optional_arg=True),
        CommandSpec("journal", "已归档 journal", _journal, takes_optional_arg=True),
        CommandSpec("health", "健康快照", _health, takes_optional_arg=True),
        CommandSpec("doctor", "后端体检", _doctor),
        CommandSpec("audit", "任务和 worktree 审计", _audit, takes_optional_arg=True),
        CommandSpec("security", "安全审计", _security, takes_optional_arg=True),
        CommandSpec("ratelimit", "最近 429 限流", _ratelimit, takes_optional_arg=True),
        CommandSpec("metrics", "最近任务趋势", _metrics, takes_optional_arg=True),
        CommandSpec("log", "最近 job 日志摘要", _log, takes_optional_arg=True),
        CommandSpec("meta", "job.json 状态", _meta, takes_optional_arg=True),
        CommandSpec("smoke", "端到端验收", _smoke),
        CommandSpec("editcheck", "临时 repo 真改文件", _editcheck),
        CommandSpec("maintain", "自维护检查", _maintain, takes_optional_arg=True),
        CommandSpec("clean", "清理旧任务", _clean, takes_optional_arg=True),
        CommandSpec("diag", "一键诊断包", _diag, takes_optional_arg=True),
        # Fast-path host ops (bypass Codex; run local host commands).
        CommandSpec("load", "本机负载快照 (host)", ops_handlers._load),
        CommandSpec("vps", "同上 (alias of /load)", ops_handlers._vps),
        CommandSpec("htop", "top 快照 (htop-style)", ops_handlers._htop),
        CommandSpec("ps", "进程快照 (comm 模式)", ops_handlers._ps, takes_optional_arg=True),
        CommandSpec("tools", "列出 agent 工具", _tools),
        CommandSpec("disk", "磁盘快照", _tool_disk),
        CommandSpec("logs", "服务 journal 日志", _tool_logs, takes_optional_arg=True),
        CommandSpec("service_status", "Conveyor 服务状态", _tool_service_status),
        CommandSpec("git_status", "Workspace git status", _tool_git_status),
        # P5.0: Execution nodes
        CommandSpec("nodes", "Execution nodes (VPS + desktop stub)", _nodes),
        CommandSpec("node_status", "Execution nodes (alias of /nodes)", _node_status),
        CommandSpec("computer_status", "Computer Use (desktop agent) stub status", _computer_status),
        CommandSpec(
            "desktop_screenshot_status",
            "Desktop screenshot observe status (P5.2 read-only)",
            _desktop_screenshot_status,
        ),
        CommandSpec(
            "screenshot_status",
            "Desktop screenshot observe status (alias)",
            _desktop_screenshot_status,
        ),
        CommandSpec(
            "observe_request",
            "Create remote desktop observe request (P5.3)",
            _observe_request,
            takes_optional_arg=True,
        ),
        CommandSpec(
            "screenshot_request",
            "Create remote desktop observe request (alias)",
            _observe_request,
            takes_optional_arg=True,
        ),
        CommandSpec(
            "request_screenshot",
            "Create remote desktop observe request (alias)",
            _observe_request,
            takes_optional_arg=True,
        ),
        CommandSpec(
            "observe_status",
            "Recent observe requests and metadata (P5.3)",
            _observe_status,
        ),
        CommandSpec(
            "observe_cancel",
            "Cancel pending/claimed observe request (P5.3)",
            _observe_cancel,
            takes_arg=True,
        ),
        CommandSpec(
            "observe_upload",
            "Request manual thumbnail upload for observe request (P5.4)",
            _observe_upload,
            takes_arg=True,
        ),
        CommandSpec(
            "screenshot_upload",
            "Request manual thumbnail upload for screenshot ID (P5.4)",
            _screenshot_upload,
            takes_arg=True,
        ),
        CommandSpec(
            "upload_status",
            "Show recent upload requests and status (P5.4)",
            _upload_status,
        ),
        CommandSpec(
            "upload_cancel",
            "Cancel pending/claimed upload request (P5.4)",
            _upload_cancel,
            takes_arg=True,
        ),
        CommandSpec(
            "upload_cleanup",
            "Clean up VPS temporary uploaded thumbnails (P5.4)",
            _upload_cleanup,
        ),
        CommandSpec("diagnose", "Hybrid 主机诊断", _diagnose, takes_optional_arg=True),
        CommandSpec("restart", "重启 Conveyor 服务 (需确认)", _restart, takes_arg=True),
        CommandSpec("note", "添加本地笔记 (立即执行, 审计)", _note, takes_arg=True),
        CommandSpec("notes", "搜索/列出本地笔记", _notes, takes_optional_arg=True),
        CommandSpec("remind", "创建本地提醒 (立即执行, 审计)", _remind, takes_arg=True),
        CommandSpec("reminders", "列出本地提醒", _reminders, takes_optional_arg=True),
        CommandSpec("gmail_status", "Gmail 连接状态", _gmail_status),
        CommandSpec("gmail_recent", "最近邮件", _gmail_recent, takes_optional_arg=True),
        CommandSpec("gmail_search", "搜索邮件", _gmail_search, takes_arg=True),
        CommandSpec("gmail_read", "读取邮件", _gmail_read, takes_arg=True),
        CommandSpec("email_send", "发送邮件 (需确认)", _email_send, takes_arg=True),
        # Google OAuth / Calendar / Contacts (P3.4)
        CommandSpec("google_status", "Google OAuth 状态", _google_status),
        CommandSpec("auth_google", "Google OAuth 授权", _auth_google, takes_optional_arg=True),
        CommandSpec("google_revoke", "撤销 Google 授权 (需确认)", _google_revoke),
        CommandSpec("calendar_status", "Calendar 连接状态", _calendar_status),
        CommandSpec("calendar_today", "今日日程", _calendar_today),
        CommandSpec("calendar_tomorrow", "明日日程", _calendar_tomorrow),
        CommandSpec("calendar_week", "本周日程", _calendar_week),
        CommandSpec("calendar_search", "搜索日程", _calendar_search, takes_arg=True),
        CommandSpec("calendar_freebusy", "查询忙闲", _calendar_freebusy, takes_arg=True),
        CommandSpec("calendar_create", "创建日程 (需确认)", _calendar_create, takes_arg=True),
        CommandSpec("contacts_search", "搜索联系人", _contacts_search, takes_arg=True),
        # GitHub (P3.6)
        CommandSpec("github_status", "GitHub 连接状态", _github_status),
        CommandSpec("github_issues", "列出 Issues", _github_issues, takes_optional_arg=True),
        CommandSpec("github_issue", "查看 Issue 详情", _github_issue, takes_arg=True),
        CommandSpec("github_prs", "列出 Pull Requests", _github_prs, takes_optional_arg=True),
        CommandSpec("github_pr", "查看 PR 详情", _github_pr, takes_arg=True),
        CommandSpec("github_ci", "CI 状态", _github_ci, takes_optional_arg=True),
        CommandSpec("github_create_issue", "创建 Issue (审计)", _github_create_issue, takes_arg=True),
        CommandSpec("github_comment", "评论 Issue/PR (需确认)", _github_comment, takes_arg=True),
        # Planner (P3.7)
        CommandSpec("plan_today", "今日优先级分析", _plan_today),
        CommandSpec("plan_dev", "开发计划", _plan_dev),
        CommandSpec("planner_health", "Planner 健康检查", _plan_health),
        CommandSpec("inbox_triage", "邮件分类整理", _plan_triage),
        CommandSpec("schedule_review", "日程审查", _plan_schedule),
        CommandSpec("planners", "列出 Planner Profiles", _planners),
        # Project Profiles (P3.9)
        CommandSpec("projects", "列出项目", _projects),
        CommandSpec("project_add", "添加项目 (审计)", _project_add, takes_arg=True),
        CommandSpec("project_use", "切换活跃项目 (审计)", _project_use, takes_arg=True),
        CommandSpec("project_show", "查看项目详情", _project_show, takes_optional_arg=True),
        CommandSpec("project_remove", "删除项目 (需确认)", _project_remove, takes_arg=True),
        CommandSpec("project_status", "项目状态分析", _project_status, takes_optional_arg=True),
        CommandSpec("project_health", "项目健康检查", _project_health, takes_optional_arg=True),
        CommandSpec("project_roadmap", "项目 Roadmap", _project_roadmap, takes_optional_arg=True),
        CommandSpec("project_next", "项目下一步行动", _project_next, takes_optional_arg=True),
        CommandSpec("project_release_checklist", "发布清单", _project_release_checklist, takes_optional_arg=True),
        CommandSpec("project_brief", "项目简报", _project_brief, takes_optional_arg=True),
        # Setup Wizard (P3.10)
        CommandSpec("setup", "设置向导", _setup),
        CommandSpec("setup_status", "配置状态", _setup_status),
        CommandSpec("setup_check", "设置检查清单", _setup_check),
        CommandSpec("setup_project", "项目配置指南", _setup_project),
        CommandSpec("setup_gmail", "Gmail 配置指南", _setup_gmail),
        CommandSpec("setup_google", "Google 配置指南", _setup_google),
        CommandSpec("setup_github", "GitHub 配置指南", _setup_github),
        # Project Import/Export (P3.11)
        CommandSpec("project_export", "导出项目为 JSON", _project_export, takes_optional_arg=True),
        CommandSpec("project_export_all", "导出所有项目", _project_export_all),
        CommandSpec("project_import", "从 JSON 导入项目", _project_import, takes_arg=True),
        CommandSpec("project_template", "项目模板", _project_template, takes_optional_arg=True),
        # File Search / Knowledge Base (P4.2)
        CommandSpec("files_roots", "列出搜索根目录", _files_roots),
        CommandSpec("files_search", "搜索文件", _files_search, takes_arg=True),
        CommandSpec("files_read", "读取文件", _files_read, takes_arg=True),
        CommandSpec("kb_index", "索引知识库", _kb_index),
        CommandSpec("kb_status", "知识库状态", _kb_status),
        CommandSpec("kb_search", "搜索知识库", _kb_search, takes_arg=True),
        CommandSpec("project_docs", "搜索项目文档", _project_docs, takes_arg=True),
        CommandSpec("kb_collect_facts", "收集本地文档证据", _kb_collect_facts, takes_arg=True),
        # Web Fetch / Search / Research (P4.1)
        CommandSpec("web_fetch", "获取网页内容", _web_fetch, takes_arg=True),
        CommandSpec("web_text", "获取网页文本", _web_text, takes_arg=True),
        CommandSpec("web_headers", "获取 HTTP headers", _web_headers, takes_arg=True),
        CommandSpec("web_search", "Web 搜索", _web_search, takes_arg=True),
        CommandSpec("research", "Web 研究", _research, takes_arg=True),
        CommandSpec("project_research", "项目研究", _project_research, takes_arg=True),
        # Job Queue (P3.8)
        CommandSpec("queue", "查看队列状态", _queue_status),
        CommandSpec("queue_cancel", "取消队列任务", _queue_cancel, takes_arg=True),
        CommandSpec("queue_clear", "清空队列", _queue_clear),
        CommandSpec("queue_pause", "暂停队列", _queue_pause),
        CommandSpec("queue_resume", "恢复队列", _queue_resume),
        # Daily Briefing (P3.5)
        CommandSpec("brief_today", "今日简报", _brief_today),
        CommandSpec("brief_tomorrow", "明日简报", _brief_tomorrow),
        CommandSpec("brief_settings", "简报设置状态", _brief_settings),
        CommandSpec("brief_enable", "启用每日简报", _brief_enable, takes_optional_arg=True),
        CommandSpec("brief_disable", "禁用每日简报 (需确认)", _brief_disable),
        CommandSpec("brief_probe", "简报探针 (dry-run)", _brief_probe),
        CommandSpec("scheduler_status", "提醒调度器状态报告", _scheduler_status),
        CommandSpec("scheduler_probe", "调度器 dry-run 探测", _scheduler_probe),
        CommandSpec("scheduler_probe_live", "调度器实时投递测试 (需确认)", _scheduler_probe_live),
        CommandSpec("audit_tools", "危险工具审计日志", _audit_tools, takes_optional_arg=True),
        CommandSpec("deploy_status", "部署状态", _deploy_status),
        CommandSpec(
            "deploy_verify",
            "P5.2 desktop screenshot observe deploy readiness (read-only)",
            _deploy_verify,
        ),
        CommandSpec("context", "查看最近会话上下文", _context),
        CommandSpec("forget", "清除当前会话记录", _forget),
        CommandSpec("help", "帮助", _help),
        CommandSpec("nl_help", "自然语言示例", _nl_help),
    ]
}


def parse_command(text: str) -> tuple[str, str] | None:
    """Strip leading slash, return (name, rest). None if not a command."""
    s = text.strip()
    if not s.startswith("/"):
        return None
    name, _, rest = s.partition(" ")
    name = name.lstrip("/").lower()
    if not name:
        return None
    return name, rest.strip()


async def run_command(
    cmd_name: str,
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    settings: Settings,
    arg: str,
) -> bool:
    spec = COMMAND_TABLE.get(cmd_name)
    if not spec:
        return False
    if spec.takes_arg and not arg:
        await port.reply(msg, f"Usage: /{cmd_name} <arg>")
        return True
    await spec.handler(msg, port, runner, settings, arg)
    return True
