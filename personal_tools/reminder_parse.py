"""personal_tools/reminder_parse.py — simple natural-language due-time parsing."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_IN_MINUTES = re.compile(r"\bin\s+(\d+)\s*m(?:in(?:ute)?s?)?\b", re.IGNORECASE)
_IN_HOURS = re.compile(r"\bin\s+(\d+)\s*h(?:our?s?)?\b", re.IGNORECASE)
_TOMORROW = re.compile(
    r"\btomorrow\s+(\d{1,2}):(\d{2})\b",
    re.IGNORECASE,
)
_ISO = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)\b"
)

REMIND_USAGE = (
    "用法: /remind <内容> [时间]\n"
    "时间格式:\n"
    "  in 10m / in 2h\n"
    "  tomorrow HH:MM\n"
    "  ISO 时间 (2026-06-16T09:00:00)\n"
    "示例:\n"
    "  /remind in 10m 取快递\n"
    "  /remind 站会 tomorrow 09:00\n"
    "  /remind 2026-06-16T09:00:00 提交报告"
)


def parse_reminder_text(raw: str, *, tz_name: str) -> tuple[str, datetime] | None:
    """Return (body, due_at) or None if no time token found."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)

    m = _IN_MINUTES.search(text)
    if m:
        minutes = int(m.group(1))
        body = _strip_match(text, m)
        due = now_local + timedelta(minutes=minutes)
        return body, due.astimezone(timezone.utc)

    m = _IN_HOURS.search(text)
    if m:
        hours = int(m.group(1))
        body = _strip_match(text, m)
        due = now_local + timedelta(hours=hours)
        return body, due.astimezone(timezone.utc)

    m = _TOMORROW.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return None
        body = _strip_match(text, m)
        tomorrow = (now_local + timedelta(days=1)).date()
        due = datetime(
            tomorrow.year,
            tomorrow.month,
            tomorrow.day,
            hour,
            minute,
            tzinfo=tz,
        )
        return body.strip(), due.astimezone(timezone.utc)

    m = _ISO.search(text)
    if m:
        token = m.group(1).replace(" ", "T")
        body = _strip_match(text, m)
        if token.endswith("Z"):
            due = datetime.fromisoformat(token.replace("Z", "+00:00"))
        elif "+" in token[10:] or "-" in token[10:]:
            due = datetime.fromisoformat(token)
        else:
            due = datetime.fromisoformat(token).replace(tzinfo=tz)
        return body.strip(), due.astimezone(timezone.utc)

    return None


def _strip_match(text: str, match: re.Match[str]) -> str:
    before = text[: match.start()].strip()
    after = text[match.end() :].strip()
    if before and after:
        return f"{before} {after}".strip()
    return (before or after).strip()
