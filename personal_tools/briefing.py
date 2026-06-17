"""personal_tools/briefing.py — Daily Briefing aggregator for Conveyor (P3.5+).

Builds a daily briefing by aggregating:
  - Calendar events (if Google OAuth configured)
  - Due/pending reminders
  - Recent Gmail messages (if Gmail configured)
  - Recent notes
  - GitHub summary (if configured, P3.6)

All outputs pass redact_text + truncate. No raw OAuth tokens or passwords
are ever exposed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.store import PersonalToolsStore
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# Default user timezone if not configured
DEFAULT_TZ = "America/Toronto"


def _get_user_tz(settings: Settings) -> ZoneInfo:
    """Get user timezone from settings."""
    tz_name = settings.user_timezone or DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _local_date_str(settings: Settings, dt: datetime | None = None) -> str:
    """Get local date string in YYYY-MM-DD format."""
    tz = _get_user_tz(dt.astimezone(tz) if dt else datetime.now(tz))
    return dt.strftime("%Y-%m-%d")


def briefing_status(settings: Settings, operator_id: str) -> ToolResult:
    """Report briefing settings status."""
    store = PersonalToolsStore(settings)
    bs = store.get_briefing_settings(operator_id)

    if bs is None or not bs.enabled:
        return ToolResult(ok=True, text=(
            "📋 Daily Briefing 未启用\n"
            "使用 /brief_enable [HH:MM] 启用（默认 09:00）"
        ))

    return ToolResult(ok=True, text=(
        "📋 Daily Briefing 已启用\n"
        f"时间: {bs.local_time}\n"
        f"通道: {bs.channel or '(未设置)'}\n"
        f"chat_id: {bs.chat_id[:8]}..." if bs.chat_id else "chat_id: (未设置)"
    ))


def briefing_enable(
    settings: Settings,
    operator_id: str,
    channel: str,
    chat_id: str,
    local_time: str = "09:00",
) -> ToolResult:
    """Enable daily briefing with specified settings."""
    # Validate time format
    try:
        parts = local_time.split(":")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except (ValueError, IndexError):
        return ToolResult(ok=False, text="⚠️ 时间格式无效，请使用 HH:MM（如 09:00）")

    store = PersonalToolsStore(settings)
    store.update_briefing_settings(
        operator_id,
        enabled=True,
        local_time=local_time,
        channel=channel,
        chat_id=chat_id,
    )

    return ToolResult(ok=True, text=(
        "✅ Daily Briefing 已启用\n"
        f"时间: 每天 {local_time}\n"
        f"通道: {channel}"
    ))


def briefing_disable(settings: Settings, operator_id: str) -> ToolResult:
    """Disable daily briefing."""
    store = PersonalToolsStore(settings)
    bs = store.get_briefing_settings(operator_id)

    if bs is None or not bs.enabled:
        return ToolResult(ok=True, text="ℹ️ Daily Briefing 未启用")

    store.update_briefing_settings(operator_id, enabled=False)
    return ToolResult(ok=True, text="✅ Daily Briefing 已禁用")


def briefing_build(
    settings: Settings,
    operator_id: str,
    target_date: str | None = None,
) -> ToolResult:
    """Build a briefing for the given date (YYYY-MM-DD).

    If target_date is None, uses today in user's timezone.
    """
    tz = _get_user_tz(settings)
    now = datetime.now(tz)

    if target_date:
        try:
            target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            return ToolResult(ok=False, text="⚠️ 日期格式无效，请使用 YYYY-MM-DD")
    else:
        target = now

    date_str = target.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][target.weekday()]

    lines = [f"📋 Daily Briefing — {date_str} ({weekday})", ""]

    # 1. Calendar events (if Google OAuth configured)
    cal_section = _build_calendar_section(settings, target)
    lines.append(cal_section)
    lines.append("")

    # 2. Due/pending reminders
    rem_section = _build_reminders_section(settings, operator_id, target)
    lines.append(rem_section)
    lines.append("")

    # 3. Recent Gmail messages (if configured)
    gmail_section = _build_gmail_section(settings)
    lines.append(gmail_section)
    lines.append("")

    # 4. Recent notes
    notes_section = _build_notes_section(settings, operator_id)
    lines.append(notes_section)
    lines.append("")

    # 5. GitHub summary (if configured, P3.6)
    github_section = _build_github_section(settings)
    lines.append(github_section)
    lines.append("")

    # 6. Active projects (P3.9)
    projects_section = _build_projects_section(settings, operator_id)
    lines.append(projects_section)

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def briefing_today(settings: Settings, operator_id: str) -> ToolResult:
    """Build briefing for today."""
    return briefing_build(settings, operator_id)


def briefing_tomorrow(settings: Settings, operator_id: str) -> ToolResult:
    """Build briefing for tomorrow."""
    tz = _get_user_tz(settings)
    tomorrow = datetime.now(tz) + timedelta(days=1)
    return briefing_build(settings, operator_id, tomorrow.strftime("%Y-%m-%d"))


def briefing_probe(settings: Settings, operator_id: str) -> ToolResult:
    """Dry-run probe: build a synthetic briefing without sending."""
    result = briefing_build(settings, operator_id)
    # Prefix with probe indicator
    if result.ok:
        return ToolResult(ok=True, text=(
            "🔍 Briefing Probe (dry-run)\n"
            "以下为模拟简报内容，未实际发送:\n\n" + result.text
        ))
    return result


def briefing_check_and_send(settings: Settings, now_utc: datetime | None = None) -> int:
    """Check all enabled briefings and send if due.

    Returns count of briefings sent.

    Called by scheduler_tick.py on each timer tick.
    """
    store = PersonalToolsStore(settings)
    enabled = store.list_enabled_briefings()

    if not enabled:
        return 0

    sent_count = 0
    now = now_utc or datetime.now(timezone.utc)

    for bs in enabled:
        if not bs.channel or not bs.chat_id:
            logger.warning("Briefing %s: missing channel/chat_id, skipping", bs.operator_id)
            continue

        # Check if already sent for today in user's timezone
        tz = _get_user_tz(settings)
        local_now = now.astimezone(tz)
        local_date = local_now.strftime("%Y-%m-%d")

        if store.has_briefing_run_for_date(bs.operator_id, local_date):
            continue

        # Check if it's time to send
        try:
            h, m = map(int, bs.local_time.split(":"))
            target_time = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if local_now < target_time:
                continue  # Not time yet
        except (ValueError, IndexError):
            logger.warning("Briefing %s: invalid local_time %s", bs.operator_id, bs.local_time)
            continue

        # Build and send briefing
        try:
            result = briefing_build(settings, bs.operator_id, local_date)
            if result.ok:
                _send_briefing(bs.channel, bs.chat_id, result.text, settings)
                store.record_briefing_run(bs.operator_id, local_date, status="sent")
                sent_count += 1
                logger.info("Briefing sent to %s for %s", bs.operator_id, local_date)
            else:
                store.record_briefing_run(bs.operator_id, local_date, status="failed", error=result.text)
                logger.error("Briefing build failed for %s: %s", bs.operator_id, result.text)
        except Exception as exc:
            store.record_briefing_run(bs.operator_id, local_date, status="failed", error=str(exc))
            logger.error("Briefing send failed for %s: %s", bs.operator_id, exc)

    return sent_count


def _send_briefing(channel: str, chat_id: str, text: str, settings: Settings) -> None:
    """Send briefing via the appropriate channel.

    Currently only Telegram is implemented. Feishu can be added later.
    """
    if channel == "telegram":
        # Import here to avoid circular imports
        import asyncio
        try:
            from channel.telegram_adapter import send_telegram_message
            asyncio.get_event_loop().run_until_complete(
                send_telegram_message(chat_id, text, settings.telegram_bot_token)
            )
        except Exception as exc:
            logger.error("Failed to send Telegram briefing: %s", exc)
            raise
    else:
        logger.warning("Briefing channel %s not implemented yet", channel)
        raise NotImplementedError(f"Channel {channel} not implemented")


def _build_calendar_section(settings: Settings, target: datetime) -> str:
    """Build calendar events section."""
    try:
        from personal_tools.google_oauth import load_credentials
        creds = load_credentials(settings)
        if not creds:
            return "📅 日历: 未配置 Google OAuth（跳过）"

        from personal_tools.calendar_google import calendar_today, calendar_tomorrow
        if target.date() == datetime.now(target.tzinfo).date():
            result = calendar_today(settings)
        else:
            result = calendar_tomorrow(settings)

        if result.ok:
            return result.text
        return f"📅 日历: {result.text}"
    except ImportError:
        return "📅 日历: Google 依赖未安装（跳过）"
    except Exception as exc:
        return f"📅 日历: 获取失败 ({redact_text(str(exc))})"


def _build_reminders_section(settings: Settings, operator_id: str, target: datetime) -> str:
    """Build reminders section for the target date."""
    try:
        store = PersonalToolsStore(settings)
        reminders = store.list_due_reminders(operator_id, now=target)

        if not reminders:
            return "⏰ 提醒: 无到期提醒"

        lines = [f"⏰ 提醒 ({len(reminders)} 条):"]
        for r in reminders[:5]:  # Limit to 5
            lines.append(f"  • {r.text}")

        if len(reminders) > 5:
            lines.append(f"  ... 还有 {len(reminders) - 5} 条")

        return "\n".join(lines)
    except Exception as exc:
        return f"⏰ 提醒: 获取失败 ({redact_text(str(exc))})"


def _build_gmail_section(settings: Settings) -> str:
    """Build recent Gmail messages section."""
    try:
        if not settings.gmail_address or not settings.gmail_app_password:
            return "📧 邮件: Gmail 未配置（跳过）"

        from personal_tools.gmail_imap import gmail_recent
        result = gmail_recent(settings, limit=3)

        if result.ok:
            return result.text
        return f"📧 邮件: {result.text}"
    except ImportError:
        return "📧 邮件: Gmail 模块未加载（跳过）"
    except Exception as exc:
        return f"📧 邮件: 获取失败 ({redact_text(str(exc))})"


def _build_notes_section(settings: Settings, operator_id: str) -> str:
    """Build recent notes section."""
    try:
        store = PersonalToolsStore(settings)
        notes = store.list_recent_notes(operator_id, limit=3)

        if not notes:
            return "📝 笔记: 无最近笔记"

        lines = [f"📝 最近笔记 ({len(notes)} 条):"]
        for n in notes:
            # Truncate long notes
            preview = n.text[:50] + "..." if len(n.text) > 50 else n.text
            lines.append(f"  • {preview}")

        return "\n".join(lines)
    except Exception as exc:
        return f"📝 笔记: 获取失败 ({redact_text(str(exc))})"


def _build_github_section(settings: Settings) -> str:
    """Build GitHub summary section."""
    try:
        if not settings.github_token or not settings.github_default_repo:
            return "🐙 GitHub: 未配置（跳过）"

        from personal_tools.github_tools import github_summary
        summary = github_summary(settings)

        parts = ["🐙 GitHub:"]
        if summary.get("open_issues") is not None:
            parts.append(f"  Open Issues: {summary['open_issues']}")
        if summary.get("open_prs") is not None:
            parts.append(f"  Open PRs: {summary['open_prs']}")
        if summary.get("ci_status"):
            parts.append(f"  CI: {summary['ci_status']}")

        if len(parts) == 1:
            return "🐙 GitHub: 获取失败"

        return "\n".join(parts)
    except ImportError:
        return "🐙 GitHub: 模块未加载（跳过）"
    except Exception as exc:
        return f"🐙 GitHub: 获取失败 ({redact_text(str(exc))})"


def _build_projects_section(settings: Settings, operator_id: str) -> str:
    """Build active projects section for briefing (P3.9)."""
    try:
        from personal_tools.projects import build_project_briefing_section
        return build_project_briefing_section(settings, operator_id)
    except ImportError:
        return "📂 项目: 模块未加载（跳过）"
    except Exception as exc:
        return f"📂 项目: 获取失败 ({redact_text(str(exc))})"


# --- Adapters for personal_tools/registry.py ---

async def briefing_status_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return briefing_status(settings, operator_id)


async def briefing_today_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return briefing_today(settings, operator_id)


async def briefing_tomorrow_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return briefing_tomorrow(settings, operator_id)


async def briefing_enable_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    channel = kw.get("channel", "")
    chat_id = kw.get("chat_id", "")
    local_time = arg.strip() if arg.strip() else "09:00"
    return briefing_enable(settings, operator_id, channel, chat_id, local_time)


async def briefing_disable_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return briefing_disable(settings, operator_id)


async def briefing_probe_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return briefing_probe(settings, operator_id)


def _local_date_str(settings: Settings, dt: datetime | None = None) -> str:
    """Get local date string in YYYY-MM-DD format."""
    tz = _get_user_tz(settings)
    local_dt = (dt or datetime.now(timezone.utc)).astimezone(tz)
    return local_dt.strftime("%Y-%m-%d")
