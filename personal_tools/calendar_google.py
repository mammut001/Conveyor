"""calendar_google.py — Google Calendar tools for Conveyor personal tools.

Uses Google Calendar API via OAuth credentials.
calendar.create requires WRITE confirmation; all others are READ.

Dependencies: google-auth, google-api-python-client.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.google_oauth import build_google_service, load_credentials
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# RFC3339 format for Google Calendar API
RFC3339_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime(RFC3339_FMT)


def _today_start(tz: timezone = timezone.utc) -> datetime:
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _format_event(event: dict) -> str:
    """Format a Google Calendar event for display."""
    summary = event.get("summary", "(无标题)")
    start = event.get("start", {})
    end = event.get("end", {})

    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))

    # Shorten datetime display
    if "T" in start_str:
        try:
            dt = datetime.fromisoformat(start_str)
            start_str = dt.strftime("%m-%d %H:%M")
        except Exception:
            pass
    if "T" in end_str:
        try:
            dt = datetime.fromisoformat(end_str)
            end_str = dt.strftime("%H:%M")
        except Exception:
            pass

    location = event.get("location", "")
    loc_part = f" 📍{location}" if location else ""

    return f"  {start_str}–{end_str} | {summary}{loc_part}"


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------

def calendar_status(settings: Settings) -> ToolResult:
    """Check Google Calendar connection status."""
    creds = load_credentials(settings)
    if creds is None:
        return ToolResult(ok=False, text="⚠️ Google OAuth 未授权，请先运行 /auth_google")

    service, err = build_google_service(settings, "calendar", "v3")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    try:
        cal_list = service.calendarList().list(maxResults=5).execute()
        calendars = cal_list.get("items", [])
        lines = ["✅ Google Calendar 连接正常", ""]
        for cal in calendars:
            primary = " ⭐" if cal.get("primary") else ""
            lines.append(f"  • {cal.get('summary', '?')}{primary}")
        return ToolResult(ok=True, text="\n".join(lines))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ Calendar API 错误: {redact_text(str(exc))}")


def calendar_today(settings: Settings) -> ToolResult:
    """List today's calendar events."""
    return _list_events(settings, days=1, label="今日")


def calendar_tomorrow(settings: Settings) -> ToolResult:
    """List tomorrow's calendar events."""
    return _list_events(settings, days=1, offset_days=1, label="明日")


def calendar_week(settings: Settings) -> ToolResult:
    """List this week's calendar events."""
    return _list_events(settings, days=7, label="本周")


def calendar_search(settings: Settings, query: str) -> ToolResult:
    """Search calendar events by keyword."""
    if not query.strip():
        return ToolResult(ok=False, text="⚠️ 搜索关键词不能为空")

    service, err = build_google_service(settings, "calendar", "v3")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    try:
        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=30)).strftime(RFC3339_FMT)
        time_max = (now + timedelta(days=90)).strftime(RFC3339_FMT)

        result = service.events().list(
            q=query.strip(),
            timeMin=time_min,
            timeMax=time_max,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return ToolResult(ok=True, text=f"🔍 未找到匹配 '{query}' 的日程")

        lines = [f"🔍 搜索 '{query}' 找到 {len(events)} 条日程:", ""]
        for ev in events:
            lines.append(_format_event(ev))

        return ToolResult(ok=True, text=truncate("\n".join(lines)))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {redact_text(str(exc))}")


def calendar_freebusy(settings: Settings, query: str) -> ToolResult:
    """Check free/busy status for a time range.

    Query format: "2026-06-16 14:00-16:00" or "today 14:00-16:00"
    """
    service, err = build_google_service(settings, "calendar", "v3")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    time_min, time_max, parse_err = _parse_time_range(query)
    if parse_err:
        return ToolResult(ok=False, text=f"⚠️ {parse_err}")

    try:
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get("primary", {}).get("busy", [])

        if not busy:
            return ToolResult(ok=True, text=f"✅ 该时段空闲\n{time_min[:16]} — {time_max[:16]}")

        lines = [f"📅 该时段有 {len(busy)} 个忙块:", ""]
        for slot in busy:
            s = slot.get("start", "?")[:16]
            e = slot.get("end", "?")[:16]
            lines.append(f"  {s} — {e}")

        return ToolResult(ok=True, text="\n".join(lines))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 查询失败: {redact_text(str(exc))}")


def calendar_create(settings: Settings, arg: str) -> ToolResult:
    """Create a calendar event. Requires WRITE confirmation.

    Arg format: <title> | <datetime or range> | <description/location>
    """
    service, err = build_google_service(settings, "calendar", "v3")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    parts = [p.strip() for p in arg.split("|")]
    if len(parts) < 2:
        return ToolResult(ok=False, text=(
            "⚠️ 用法: /calendar_create <标题> | <时间> | <描述/地点>\n"
            "示例: /calendar_create 周会 | 明天 14:00-15:00 | 讨论 Q3 计划"
        ))

    title = parts[0]
    time_str = parts[1]
    desc_loc = parts[2] if len(parts) > 2 else ""

    if not title:
        return ToolResult(ok=False, text="⚠️ 标题不能为空")

    start_dt, end_dt, parse_err = _parse_event_time(time_str)
    if parse_err:
        return ToolResult(ok=False, text=f"⚠️ {parse_err}")

    # Build event body
    event_body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
    }
    if desc_loc:
        # Heuristic: if it looks like a location (short, no sentence), put in location
        if len(desc_loc) < 50 and ("楼" in desc_loc or "room" in desc_loc.lower() or "室" in desc_loc):
            event_body["location"] = desc_loc
        else:
            event_body["description"] = desc_loc

    try:
        created = service.events().insert(calendarId="primary", body=event_body).execute()
        link = created.get("htmlLink", "")
        return ToolResult(ok=True, text=(
            f"✅ 日程已创建: {title}\n"
            f"时间: {start_dt.strftime('%m-%d %H:%M')} — {end_dt.strftime('%H:%M')}\n"
            f"链接: {link}"
        ))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 创建失败: {redact_text(str(exc))}")


def calendar_create_preview(settings: Settings, arg: str) -> ToolResult:
    """Preview a calendar event creation (for confirmation display)."""
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) < 2:
        return ToolResult(ok=False, text="⚠️ 参数不足")

    title = parts[0]
    time_str = parts[1]
    desc_loc = parts[2] if len(parts) > 2 else ""

    start_dt, end_dt, parse_err = _parse_event_time(time_str)
    if parse_err:
        return ToolResult(ok=False, text=f"⚠️ {parse_err}")

    lines = [
        "📅 日程创建预览",
        "",
        f"标题: {title}",
        f"时间: {start_dt.strftime('%Y-%m-%d %H:%M')} — {end_dt.strftime('%H:%M')}",
    ]
    if desc_loc:
        lines.append(f"描述/地点: {desc_loc}")
    lines.append("")
    lines.append("确认后将创建到 Google Calendar")

    return ToolResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# Adapters for personal_tools/registry.py
# ---------------------------------------------------------------------------

async def calendar_status_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_status(settings)


async def calendar_today_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_today(settings)


async def calendar_tomorrow_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_tomorrow(settings)


async def calendar_week_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_week(settings)


async def calendar_search_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_search(settings, arg.strip())


async def calendar_freebusy_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_freebusy(settings, arg.strip())


async def calendar_create_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return calendar_create(settings, arg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_events(settings: Settings, days: int, offset_days: int = 0, label: str = "") -> ToolResult:
    """List events for a time period."""
    service, err = build_google_service(settings, "calendar", "v3")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    try:
        now = datetime.now(timezone.utc)
        start = now + timedelta(days=offset_days)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=days)

        result = service.events().list(
            calendarId="primary",
            timeMin=start.strftime(RFC3339_FMT),
            timeMax=end.strftime(RFC3339_FMT),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return ToolResult(ok=True, text=f"📅 {label}没有日程安排")

        lines = [f"📅 {label}日程 ({len(events)} 条):", ""]
        for ev in events:
            lines.append(_format_event(ev))

        return ToolResult(ok=True, text=truncate("\n".join(lines)))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 获取日程失败: {redact_text(str(exc))}")


def _parse_event_time(time_str: str) -> tuple[datetime, datetime, str | None]:
    """Parse event time string. Returns (start, end, error).

    Supported formats:
        "明天 14:00-15:00"
        "2026-06-16 14:00-15:00"
        "14:00-15:00" (today)
        "明天 14:00" (1 hour default)
    """
    from personal_tools.reminder_parse import _parse_zh_datetime  # reuse existing parser

    time_str = time_str.strip()
    if not time_str:
        return datetime.now(), datetime.now(), "时间不能为空"

    # Try to split range: "14:00-15:00" or "14:00 — 15:00"
    range_sep = None
    for sep in ["-", "—", "到"]:
        if sep in time_str:
            parts = time_str.split(sep, 1)
            if len(parts) == 2 and ":" in parts[1]:
                range_sep = sep
                break

    if range_sep:
        # Parse start and end
        left = time_str[:time_str.index(range_sep)].strip()
        right = time_str[time_str.index(range_sep) + len(range_sep):].strip()

        start_dt, err1 = _parse_single_time(left)
        if err1:
            return datetime.now(), datetime.now(), err1

        # End time might be just "15:00" (reuse date from start)
        end_dt, err2 = _parse_single_time(right, reference=start_dt)
        if err2:
            return datetime.now(), datetime.now(), err2

        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)

        return start_dt, end_dt, None
    else:
        # Single time: default 1 hour
        start_dt, err = _parse_single_time(time_str)
        if err:
            return datetime.now(), datetime.now(), err
        return start_dt, start_dt + timedelta(hours=1), None


def _parse_single_time(s: str, reference: datetime | None = None) -> tuple[datetime, str | None]:
    """Parse a single datetime string."""
    s = s.strip()

    # "明天 14:00", "今天 14:00"
    now = datetime.now(timezone.utc)
    if s.startswith("明天"):
        base = now + timedelta(days=1)
        s = s[2:].strip()
    elif s.startswith("后天"):
        base = now + timedelta(days=2)
        s = s[2:].strip()
    elif s.startswith("今天"):
        base = now
        s = s[2:].strip()
    elif reference:
        base = reference
    else:
        base = now

    # Try "HH:MM"
    if ":" in s and len(s) <= 5:
        try:
            h, m = s.split(":")
            dt = base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return dt, None
        except (ValueError, IndexError):
            pass

    # Try full datetime "2026-06-16 14:00"
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M"]:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt, None
        except ValueError:
            continue

    return now, f"无法解析时间: {s}"


def _parse_time_range(query: str) -> tuple[str, str, str | None]:
    """Parse time range for freebusy. Returns (timeMin_rfc3339, timeMax_rfc3339, error)."""
    start_dt, end_dt, err = _parse_event_time(query)
    if err:
        return "", "", err
    return start_dt.strftime(RFC3339_FMT), end_dt.strftime(RFC3339_FMT), None
