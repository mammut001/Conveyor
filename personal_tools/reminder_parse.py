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

_CN_NUM = r"[0-9零〇一二两三四五六七八九十]{1,4}"
_CN_IN = re.compile(rf"({_CN_NUM})\s*(分钟|个?小时)(?:之|以)?后")
_CN_AT = re.compile(
    rf"(今天|明天|后天)?\s*(早上|早晨|上午|中午|下午|傍晚|晚上|今晚)?\s*({_CN_NUM})\s*[点點时:：]"
    rf"\s*(?:(半)|({_CN_NUM})\s*分?)?"
)
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(token: str) -> int | None:
    """'8', '八', '十二', '二十三', '十五' -> int (0-99)."""
    if token.isdigit():
        return int(token)
    if "十" in token:
        tens, _, ones = token.partition("十")
        t = _CN_DIGITS.get(tens, None) if tens else 1
        o = _CN_DIGITS.get(ones, None) if ones else 0
        return None if t is None or o is None else t * 10 + o
    value = 0
    for ch in token:
        if ch not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[ch]
    return value


def _parse_chinese(text: str, now_local: datetime) -> tuple[str, datetime] | None:
    """明天早上8点 / 下午3点半 / 今晚九点十五分 / 10分钟后 / 2小时后."""
    m = _CN_IN.search(text)
    if m:
        amount = _cn_int(m.group(1))
        if amount is None:
            return None
        delta = timedelta(minutes=amount) if m.group(2) == "分钟" else timedelta(hours=amount)
        return _strip_match(text, m), now_local + delta
    m = _CN_AT.search(text)
    if not m or not (m.group(1) or m.group(2) or text[m.end(3):m.end(3) + 1] in "点點时"):
        return None
    hour = _cn_int(m.group(3))
    minute = 30 if m.group(4) else (_cn_int(m.group(5)) if m.group(5) else 0)
    if hour is None or minute is None or hour > 23 or minute > 59:
        return None
    period = m.group(2) or ""
    if period in ("下午", "傍晚", "晚上", "今晚") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 6:
        hour += 12
    day = {"今天": 0, "明天": 1, "后天": 2}.get(m.group(1) or "", 0)
    base = (now_local + timedelta(days=day)).date()
    due = datetime(base.year, base.month, base.day, hour, minute, tzinfo=now_local.tzinfo)
    if not m.group(1) and period != "今晚" and due <= now_local:
        due += timedelta(days=1)  # "下午3点" said after 3pm means tomorrow
    return _strip_match(text, m), due


REMIND_USAGE = (
    "用法: /remind <内容> [时间]\n"
    "时间格式:\n"
    "  in 10m / in 2h\n"
    "  tomorrow HH:MM\n"
    "  ISO 时间 (2026-06-16T09:00:00)\n"
    "  中文: 10分钟后 / 2小时后 / 明天早上8点 / 下午3点半 / 今晚九点\n"
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

    cn = _parse_chinese(text, now_local)
    if cn is not None:
        body, due = cn
        return body.strip(), due.astimezone(timezone.utc)

    return None


def _strip_match(text: str, match: re.Match[str]) -> str:
    before = text[: match.start()].strip()
    after = text[match.end() :].strip()
    if before and after:
        return f"{before} {after}".strip()
    return (before or after).strip()
