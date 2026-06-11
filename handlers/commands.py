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
    await port.reply(msg, await runner.diff_text())


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
        outcome = await run_maintenance(".env", "codex-telegram-bot", clean_threshold=100, keep=keep)
    except Exception as exc:
        await port.reply(msg, f"maintain 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, outcome.summary)


async def _doctor(msg, port, _runner, settings, _arg):
    results = [
        check_systemd_active("codex-telegram-bot"),
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
        text = diagnostics_report(".env", "codex-telegram-bot", since, metrics_limit=20)
    except Exception as exc:
        await port.reply(msg, f"diag 没跑成：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, text)


async def _security(msg, port, _runner, _settings, arg):
    since = _join_arg(arg) or "1 hour ago"
    try:
        results = run_security_audit(".env", "codex-telegram-bot", since)
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
            "codex-telegram-bot",
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
        code = await run_smoke(".env", "codex-telegram-bot", notify=False)
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
}

_TOOL_EXAMPLES: dict[str, str] = {
    "load": "看看负载",
    "disk": "磁盘空间",
    "logs": "看日志",
    "service_status": "服务还在跑吗",
    "git_status": "git status",
    "service_restart": "重启 telegram bot",
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


async def _tool_disk(msg, port, _runner, settings, arg):
    from handlers.tools.runner import run_tool
    await port.reply(msg, await run_tool(settings, "disk", arg))


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


async def _help(msg, port, _runner, _settings, _arg):
    text = "Codex Bot\n"
    text += "直接发文字 → 跑 Codex（workspace-write）\n"
    text += "记 xxx / /memo xxx → 写 MEMORY.md（不经 Codex）\n"
    text += "/status /last /diff /apply /discard /cancel\n"
    text += "/jobs [n] /memory [date] [cat] /journal [n]\n"
    text += "/health [full] [json] [nosecurity] /doctor /diag [since] /audit [stale-min]\n"
    text += "/security [since] /ratelimit [n] /metrics [n] /log [sel] /meta [sel]\n"
    text += "/smoke /editcheck /maintain [keep] /clean [keep] /run /fix\n"
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
    await port.reply(msg, text)


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
        CommandSpec("diagnose", "Hybrid 主机诊断", _diagnose, takes_optional_arg=True),
        CommandSpec("restart", "重启 Conveyor 服务 (需确认)", _restart, takes_arg=True),
        CommandSpec("audit_tools", "危险工具审计日志", _audit_tools, takes_optional_arg=True),
        CommandSpec("deploy_status", "部署状态", _deploy_status),
        CommandSpec("help", "帮助", _help),
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
