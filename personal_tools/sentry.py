"""personal_tools/sentry.py — Always-On Teammate & Proactive Sentry Engine.

Continuously and autonomously monitors host health, system services, error log
bursts, git worktrees, and GitHub CI. Features intelligent state tracking,
fingerprint-based deduplication, cooldown gates, and multi-channel delivery
(Telegram, Feishu, Web).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger("conveyor.sentry")

STATE_FILE = "state/teammate_sentry_state.json"


@dataclass
class SentryAlert:
    source: str  # e.g. "host.disk", "host.cpu", "host.service", "host.logs", "repo.git", "repo.ci"
    severity: str  # "info", "warning", "critical"
    title: str
    summary: str
    fingerprint: str
    suggested_actions: list[dict[str, str]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SentryState:
    is_paused: bool = False
    paused_until: str | None = None
    muted_sources: list[str] = field(default_factory=list)
    last_patrol_at: str | None = None
    last_alert_at: dict[str, float] = field(default_factory=dict)
    recent_alerts: list[dict[str, Any]] = field(default_factory=list)
    total_alerts_count: int = 0

    @classmethod
    def load(cls, settings: Settings) -> "SentryState":
        path = settings.codex_memory_root / STATE_FILE
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                is_paused=bool(data.get("is_paused", False)),
                paused_until=data.get("paused_until"),
                muted_sources=list(data.get("muted_sources", [])),
                last_patrol_at=data.get("last_patrol_at"),
                last_alert_at=dict(data.get("last_alert_at", {})),
                recent_alerts=list(data.get("recent_alerts", [])),
                total_alerts_count=int(data.get("total_alerts_count", 0)),
            )
        except Exception as exc:
            logger.warning("Failed to load sentry state from %s: %s", path, exc)
            return cls()

    def save(self, settings: Settings) -> None:
        path = settings.codex_memory_root / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            data = asdict(self)
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.error("Failed to save sentry state to %s: %s", path, exc)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def is_effective_paused(self) -> bool:
        if not self.is_paused:
            return False
        if not self.paused_until:
            return True
        try:
            until_dt = datetime.fromisoformat(self.paused_until)
            if datetime.now(timezone.utc) >= until_dt:
                self.is_paused = False
                self.paused_until = None
                return False
            return True
        except Exception:
            return self.is_paused

    def pause(self, hours: float = 24.0) -> str:
        self.is_paused = True
        until_dt = datetime.now(timezone.utc) + timedelta(hours=hours)
        self.paused_until = until_dt.isoformat()
        return self.paused_until

    def resume(self) -> None:
        self.is_paused = False
        self.paused_until = None

    def mute(self, source: str) -> bool:
        source = source.strip().lower()
        if source not in self.muted_sources:
            self.muted_sources.append(source)
            return True
        return False

    def unmute(self, source: str) -> bool:
        source = source.strip().lower()
        if source in self.muted_sources:
            self.muted_sources.remove(source)
            return True
        return False

    def can_alert(self, fingerprint: str, cooldown_seconds: float) -> bool:
        last = self.last_alert_at.get(fingerprint, 0.0)
        return (time.time() - last) >= cooldown_seconds

    def record_alert(self, alert: SentryAlert) -> None:
        self.last_alert_at[alert.fingerprint] = time.time()
        self.total_alerts_count += 1
        entry = {
            "source": alert.source,
            "severity": alert.severity,
            "title": alert.title,
            "summary": alert.summary,
            "fingerprint": alert.fingerprint,
            "created_at": alert.created_at,
        }
        self.recent_alerts.insert(0, entry)
        if len(self.recent_alerts) > 20:
            self.recent_alerts = self.recent_alerts[:20]


# -----------------------------------------------------------------------------
# Sentry Checkers
# -----------------------------------------------------------------------------

def check_disk_space(settings: Settings) -> list[SentryAlert]:
    """Check disk usage on critical filesystem mount points."""
    alerts: list[SentryAlert] = []
    candidates = ["/", "/srv", "/home", str(settings.codex_workspace_root)]
    seen_devs: set[int] = set()

    for path_str in candidates:
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            st = p.stat()
            dev = st.st_dev
            if dev in seen_devs:
                continue
            seen_devs.add(dev)

            usage = shutil.disk_usage(p)
            if usage.total < (1024 ** 3):
                # Skip dummy or pseudo-filesystems (e.g. macOS autofs /home)
                continue
            total_gb = usage.total / (1024 ** 3)
            free_gb = usage.free / (1024 ** 3)
            used_pct = ((usage.total - usage.free) / usage.total) * 100 if usage.total > 0 else 0

            threshold_pct = getattr(settings, "sentry_disk_threshold_pct", 90.0)
            threshold_gb = getattr(settings, "sentry_disk_threshold_gb", 3.0)

            if used_pct >= threshold_pct or free_gb <= threshold_gb:
                severity = "critical" if (free_gb < 1.0 or used_pct > 95.0) else "warning"
                summary = (
                    f"挂载路径 `{path_str}` 可用空间仅剩 {free_gb:.1f} GB ({100 - used_pct:.1f}% 可用，"
                    f"总空间 {total_gb:.1f} GB，已用 {used_pct:.1f}%)。"
                )
                alerts.append(
                    SentryAlert(
                        source="host.disk",
                        severity=severity,
                        title=f"磁盘空间偏低 ({path_str})",
                        summary=summary,
                        fingerprint=f"host.disk.{path_str}",
                        suggested_actions=[
                            {"label": "清理历史旧任务与 Worktree", "command": "/clean"},
                            {"label": "查看详细磁盘挂载情况", "command": "/disk"},
                        ],
                    )
                )
        except Exception as exc:
            logger.debug("Disk check failed on %s: %s", path_str, exc)
    return alerts


def check_cpu_and_load(settings: Settings) -> list[SentryAlert]:
    """Check system load average and detect runaway processes."""
    alerts: list[SentryAlert] = []
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        return alerts

    cpus = os.cpu_count() or 1
    load_ratio = load1 / cpus
    threshold_ratio = getattr(settings, "sentry_load_threshold_ratio", 2.0)

    if load_ratio >= threshold_ratio:
        # Check top CPU hog process via ps
        top_proc_info = ""
        top_cmd = "unknown"
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid,pcpu,comm", "-r"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
                if len(lines) > 1:
                    top_proc_info = f"\n高耗能进程排名前列：\n" + "\n".join(lines[1:4])
                    parts = lines[1].split()
                    if len(parts) >= 3:
                        top_cmd = parts[2]
        except Exception:
            pass

        summary = (
            f"1分钟负载达到 {load1:.2f} ({cpus} 核 CPU，负载比例 {load_ratio:.1f}x)。"
            f"{top_proc_info}"
        )
        alerts.append(
            SentryAlert(
                source="host.cpu",
                severity="warning",
                title="系统 CPU 负载过高",
                summary=summary,
                fingerprint=f"host.cpu.high_load",
                suggested_actions=[
                    {"label": "查看实时进程快照", "command": "/ps"},
                    {"label": "运行主机系统诊断", "command": "/diagnose"},
                ],
            )
        )
    return alerts


def check_services(settings: Settings) -> list[SentryAlert]:
    """Check if monitored systemd services are active and healthy."""
    alerts: list[SentryAlert] = []
    monitored = getattr(settings, "sentry_monitored_services", ())
    if not monitored:
        return alerts

    # Quick check if systemctl exists
    if not shutil.which("systemctl"):
        return alerts

    for service in monitored:
        unit = service
        if not (unit.endswith(".service") or unit.endswith(".timer")):
            check_timer = subprocess.run(
                ["systemctl", "is-active", f"{service}.timer"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if check_timer.stdout.strip() == "active":
                unit = f"{service}.timer"

        try:
            res = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            state = res.stdout.strip()
            if state != "active":
                # Check detailed substate / failure
                fail_res = subprocess.run(
                    ["systemctl", "show", unit, "--property=ActiveState,SubState,Result"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                details = fail_res.stdout.strip().replace("\n", ", ") if fail_res.returncode == 0 else state
                alias = "telegram" if "telegram" in unit else ("feishu" if "feishu" in unit else unit)
                alerts.append(
                    SentryAlert(
                        source="host.service",
                        severity="critical",
                        title=f"系统服务异常: {unit}",
                        summary=f"服务状态为 `{state}` ({details})，未处于正常运行状态。",
                        fingerprint=f"host.service.{unit}",
                        suggested_actions=[
                            {"label": f"尝试重启服务", "command": f"/restart {alias}"},
                            {"label": f"查看最近服务日志", "command": f"/logs {unit}"},
                        ],
                    )
                )
        except Exception as exc:
            logger.debug("Service check failed on %s: %s", service, exc)
    return alerts


def check_log_error_spikes(settings: Settings) -> list[SentryAlert]:
    """Detect sudden bursts of errors in host system journal."""
    alerts: list[SentryAlert] = []
    if not shutil.which("journalctl"):
        return alerts

    threshold = getattr(settings, "sentry_error_burst_threshold", 5)
    try:
        # Check errors in the last 10 minutes
        res = subprocess.run(
            ["journalctl", "-p", "err", "--since", "10 minutes ago", "-n", "30", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
            error_count = len(lines)
            if error_count >= threshold:
                sample_errors = lines[-3:]
                summary = (
                    f"最近 10 分钟检测到 {error_count} 条错误日志，出现异常峰值。\n"
                    f"最新错误样例：\n• " + "\n• ".join(sample_errors)
                )
                alerts.append(
                    SentryAlert(
                        source="host.logs",
                        severity="warning",
                        title="日志错误突增预警",
                        summary=summary,
                        fingerprint="host.logs.error_burst",
                        suggested_actions=[
                            {"label": "查看最近服务日志", "command": "/logs"},
                            {"label": "运行深度系统诊断", "command": "/diagnose"},
                        ],
                    )
                )
    except Exception as exc:
        logger.debug("Log error check failed: %s", exc)
    return alerts


def check_repo_git(settings: Settings) -> list[SentryAlert]:
    """Check working repository status and flag abandoned dirty worktrees."""
    alerts: list[SentryAlert] = []
    ws = settings.codex_workspace_root
    if not ws.exists() or not (ws / ".git").exists():
        return alerts

    try:
        res = subprocess.run(
            ["git", "-C", str(ws), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            lines = [line for line in res.stdout.splitlines() if line.strip()]
            if len(lines) > 20:
                alerts.append(
                    SentryAlert(
                        source="repo.git",
                        severity="info",
                        title="工作区积累了较多未提交变更",
                        summary=f"主仓库 `{ws.name}` 当前有 {len(lines)} 个未提交或未跟踪文件。",
                        fingerprint=f"repo.git.{ws.name}.uncommitted",
                        suggested_actions=[
                            {"label": "查看工作区差异", "command": "/diff"},
                            {"label": "安全合入当前改动", "command": "/apply"},
                        ],
                    )
                )
    except Exception as exc:
        logger.debug("Git status check failed: %s", exc)
    return alerts


def check_github_ci(settings: Settings) -> list[SentryAlert]:
    """Check if the latest GitHub Actions CI run on main failed (when configured)."""
    alerts: list[SentryAlert] = []
    token = getattr(settings, "github_token", None)
    repo = getattr(settings, "github_repo", None)
    if not token or not repo:
        return alerts

    try:
        from personal_tools.github_tools import get_ci_status
        ci_res = get_ci_status(settings, ref="main")
        if ci_res.ok and "failure" in ci_res.text.lower():
            alerts.append(
                SentryAlert(
                    source="repo.ci",
                    severity="critical",
                    title="GitHub CI 构建失败",
                    summary=f"仓库 `{repo}` main 分支最新 CI 构建失败。\n{truncate(ci_res.text, 200)}",
                    fingerprint=f"repo.ci.{repo}.failure",
                    suggested_actions=[
                        {"label": "查看详细 CI 状态", "command": "/github_ci"},
                        {"label": "启动自动排查与修复", "command": "/fix GitHub CI failure on main"},
                    ],
                )
            )
    except Exception as exc:
        logger.debug("GitHub CI check failed: %s", exc)
    return alerts


# -----------------------------------------------------------------------------
# Patrol & Alert Delivery
# -----------------------------------------------------------------------------

def is_sentry_due(settings: Settings, state: SentryState) -> bool:
    """Check if enough time has passed since the last sentry patrol."""
    if not state.last_patrol_at:
        return True
    try:
        last_dt = datetime.fromisoformat(state.last_patrol_at)
        interval = getattr(settings, "sentry_interval_seconds", 300)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() >= interval
    except Exception:
        return True


def run_sentry_patrol(
    settings: Settings,
    *,
    force: bool = False,
    dry_run: bool = False,
    check_due: bool = False,
) -> tuple[list[SentryAlert], list[SentryAlert]]:
    """Execute a full sentry patrol across all host and repo checkers.

    Returns (alerts_to_deliver, suppressed_alerts).
    """
    if not getattr(settings, "teammate_enabled", True) and not force:
        return [], []

    state = SentryState.load(settings)
    if state.is_effective_paused() and not force:
        logger.debug("Sentry patrol skipped: teammate alerts are paused.")
        return [], []

    if check_due and not force and not is_sentry_due(settings, state):
        logger.debug("Sentry patrol skipped: not due yet.")
        return [], []

    cooldown = getattr(settings, "sentry_cooldown_seconds", 7200)
    raw_alerts: list[SentryAlert] = []

    # Run all checkers
    raw_alerts.extend(check_disk_space(settings))
    raw_alerts.extend(check_cpu_and_load(settings))
    raw_alerts.extend(check_services(settings))
    raw_alerts.extend(check_log_error_spikes(settings))
    raw_alerts.extend(check_repo_git(settings))
    raw_alerts.extend(check_github_ci(settings))

    to_deliver: list[SentryAlert] = []
    suppressed: list[SentryAlert] = []

    for alert in raw_alerts:
        # Check muted sources
        if alert.source in state.muted_sources:
            suppressed.append(alert)
            continue
        # Check cooldown
        if not force and not state.can_alert(alert.fingerprint, cooldown):
            suppressed.append(alert)
            continue

        to_deliver.append(alert)
        if not dry_run:
            state.record_alert(alert)

    if not dry_run:
        state.last_patrol_at = datetime.now(timezone.utc).isoformat()
        state.save(settings)

    return to_deliver, suppressed


def format_alert_text(alert: SentryAlert) -> str:
    """Format alert into a clean, actionable Markdown message."""
    icon = "🚨" if alert.severity == "critical" else ("⚠️" if alert.severity == "warning" else "ℹ️")
    lines = [
        f"{icon} **【全天候守护·主动预警】{alert.title}**",
        "──────────────────────",
        alert.summary,
    ]
    if alert.suggested_actions:
        lines.append("\n👉 **推荐快速操作：**")
        for act in alert.suggested_actions:
            lines.append(f"• `{act['command']}` — {act['label']}")

    lines.append(f"\n*(来源: {alert.source} · 发送 `/teammate pause` 可暂停提醒)*")
    return "\n".join(lines)


def deliver_sentry_alerts(
    settings: Settings,
    alerts: list[SentryAlert],
    *,
    dry_run: bool = False,
) -> int:
    """Deliver proactively generated sentry alerts to configured channels."""
    if not alerts:
        return 0

    delivered = 0
    for alert in alerts:
        text = format_alert_text(alert)

        if dry_run:
            logger.info("[dry-run] Would deliver proactive alert: %s", alert.title)
            delivered += 1
            continue

        # Deliver to Telegram
        tg_chat_id = getattr(settings, "telegram_allowed_user_id", None)
        if tg_chat_id and getattr(settings, "telegram_bot_token", None):
            try:
                from scripts.telegram_api import send_message
                send_message(settings, text, chat_id=int(tg_chat_id))
                delivered += 1
                logger.info("Delivered proactive alert to Telegram user %s: %s", tg_chat_id, alert.title)
            except Exception as exc:
                logger.error("Failed to send sentry alert to Telegram: %s", exc)

        # Deliver to Feishu if configured
        feishu_open_id = getattr(settings, "lark_allowed_open_id", None)
        if feishu_open_id and getattr(settings, "lark_app_id", None):
            try:
                _deliver_feishu_alert(settings, feishu_open_id, alert)
                logger.info("Delivered proactive alert to Feishu user %s: %s", feishu_open_id, alert.title)
            except Exception as exc:
                logger.debug("Feishu alert delivery fallback/skip: %s", exc)

    return delivered


def _deliver_feishu_alert(settings: Settings, open_id: str, alert: SentryAlert) -> None:
    """Deliver alert to Feishu via OpenAPI."""
    import urllib.request
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    req_data = json.dumps({
        "app_id": settings.lark_app_id,
        "app_secret": settings.lark_app_secret,
    }).encode("utf-8")
    req = urllib.request.Request(token_url, data=req_data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        token = res.get("tenant_access_token")

    if not token:
        return

    msg_url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    content = json.dumps({"text": format_alert_text(alert)})
    payload = json.dumps({
        "receive_id": open_id,
        "msg_type": "text",
        "content": content,
    }).encode("utf-8")
    send_req = urllib.request.Request(
        msg_url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    urllib.request.urlopen(send_req, timeout=10)


# -----------------------------------------------------------------------------
# Teammate Command Interface
# -----------------------------------------------------------------------------

def teammate_status_text(settings: Settings) -> str:
    """Generate comprehensive status text for /teammate command."""
    state = SentryState.load(settings)
    is_paused = state.is_effective_paused()
    status_emoji = "⏸️ 已暂停 (Paused)" if is_paused else "🟢 活跃巡检中 (Active)"

    interval = getattr(settings, "sentry_interval_seconds", 300)
    cooldown = getattr(settings, "sentry_cooldown_seconds", 7200)

    last_patrol = state.last_patrol_at[:19].replace("T", " ") if state.last_patrol_at else "尚未执行"

    services_str = ", ".join(getattr(settings, "sentry_monitored_services", ()))

    lines = [
        "🛡️ **全天候智能体队友 (Always-On Teammate)**",
        f"• 运行状态: {status_emoji}",
        f"• 巡检周期: 每 {interval} 秒 ({interval // 60} 分钟)",
        f"• 告警防骚扰冷却: {cooldown // 3600} 小时",
        f"• 上次巡检: {last_patrol}",
        f"• 累计触发告警: {state.total_alerts_count} 次",
        "",
        "📋 **守护巡检模块矩阵：**",
        f"• 💾 磁盘空间守护 (使用率 > {getattr(settings, 'sentry_disk_threshold_pct', 90)}% 或可用 < {getattr(settings, 'sentry_disk_threshold_gb', 3)} GB)",
        f"• ⚡ CPU 与异常进程守护 (系统负载比例 > {getattr(settings, 'sentry_load_threshold_ratio', 2.0)}x)",
        f"• 🛠️ 系统服务巡检 ({services_str or '无'})",
        f"• 📜 错误日志激增检测 (10分钟错误数 >= {getattr(settings, 'sentry_error_burst_threshold', 5)})",
        "• 📦 Git 工作区与 GitHub CI 守护",
    ]

    if state.muted_sources:
        lines.append(f"\n🔇 **已静音告警源：** {', '.join(state.muted_sources)}")

    if state.recent_alerts:
        lines.append("\n🕒 **最近触发的守护预警：**")
        for a in state.recent_alerts[:3]:
            ts = a.get("created_at", "")[:16].replace("T", " ")
            lines.append(f"• [{ts}] {a.get('title', 'Alert')}: {truncate(a.get('summary', ''), 60)}")

    lines.extend([
        "",
        "💡 **快捷操作：**",
        "• `/teammate check` — 立即对全系统执行一次巡检",
        "• `/teammate pause [小时]` — 暂停主动预警推送",
        "• `/teammate resume` — 恢复主动巡检",
        "• `/teammate mute <source>` — 静音指定告警源 (如 disk, cpu, logs)",
    ])
    return "\n".join(lines)


def run_teammate_command(settings: Settings, arg: str) -> str:
    """Handle /teammate and /sentry subcommands."""
    parts = arg.strip().split()
    subcmd = parts[0].lower() if parts else "status"

    state = SentryState.load(settings)

    if subcmd in ("status", ""):
        return teammate_status_text(settings)

    elif subcmd in ("check", "run", "patrol"):
        alerts, suppressed = run_sentry_patrol(settings, force=True, dry_run=True)
        lines = ["🛡️ **实时系统巡检完成**\n"]
        if not alerts and not suppressed:
            lines.append("✅ **全系统指标健康正常，未发现任何异常或报警。**")
            lines.append(f"• 磁盘、CPU 负载、核心服务及错误日志均处于安全范围内。")
        else:
            if alerts:
                lines.append(f"🚨 **发现 {len(alerts)} 项需要关注的异常：**\n")
                for a in alerts:
                    lines.append(f"• **{a.title}** ({a.severity})\n  {a.summary}")
            if suppressed:
                lines.append(f"\nℹ️ 另有 {len(suppressed)} 项处于冷却或静音期的报警。")
        return "\n".join(lines)

    elif subcmd == "pause":
        hours = 24.0
        if len(parts) > 1:
            try:
                hours = float(parts[1])
            except ValueError:
                pass
        until_iso = state.pause(hours)
        state.save(settings)
        until_display = until_iso[:16].replace("T", " ")
        return f"⏸️ 已暂停全天候队友主动提醒（持续 {hours:g} 小时，至 {until_display} UTC）。发送 `/teammate resume` 可随时恢复。"

    elif subcmd == "resume":
        state.resume()
        state.save(settings)
        return "▶️ 已恢复全天候队友的主动巡检与预警推送。"

    elif subcmd == "mute":
        if len(parts) < 2:
            return "用法：`/teammate mute <source>`（例如：`/teammate mute disk`、`/teammate mute cpu`）"
        source = parts[1].lower()
        if state.mute(source):
            state.save(settings)
            return f"🔇 已静音告警源 `{source}`。后续将不再主动推送该项预警。"
        return f"ℹ️ 告警源 `{source}` 已处于静音列表中。"

    elif subcmd == "unmute":
        if len(parts) < 2:
            return "用法：`/teammate unmute <source>`"
        source = parts[1].lower()
        if state.unmute(source):
            state.save(settings)
            return f"🔔 已取消静音告警源 `{source}`。"
        return f"⚠️ 告警源 `{source}` 不在静音列表中。"

    else:
        return f"未知子命令 `{subcmd}`。支持：`/teammate status`、`/teammate check`、`/teammate pause`、`/teammate resume`、`/teammate mute`。"


# -----------------------------------------------------------------------------
# Personal Tool Adapters
# -----------------------------------------------------------------------------

async def teammate_status_tool(settings: Settings, _arg: str, **_kwargs: Any) -> ToolResult:
    """Check Always-On Teammate sentry status."""
    return ToolResult(ok=True, text=teammate_status_text(settings))


async def teammate_check_tool(settings: Settings, _arg: str, **_kwargs: Any) -> ToolResult:
    """Run immediate proactive sentry patrol."""
    return ToolResult(ok=True, text=run_teammate_command(settings, "check"))


async def teammate_pause_tool(settings: Settings, arg: str, **_kwargs: Any) -> ToolResult:
    """Pause sentry alerts for a given number of hours."""
    return ToolResult(ok=True, text=run_teammate_command(settings, f"pause {arg}".strip()))


async def teammate_resume_tool(settings: Settings, _arg: str, **_kwargs: Any) -> ToolResult:
    """Resume sentry alerts."""
    return ToolResult(ok=True, text=run_teammate_command(settings, "resume"))


async def teammate_run_tool(settings: Settings, arg: str, **_kwargs: Any) -> ToolResult:
    """Execute generic teammate subcommand."""
    return ToolResult(ok=True, text=run_teammate_command(settings, arg))

