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
_CN_IN = re.compile(
    rf"(?:"
    rf"(?:(?P<half_only>半\s*个?\s*小时)|(?P<hours>{_CN_NUM})\s*(?P<half_hour>个半|个)?\s*小时)"
    rf"(?:\s*(?P<minutes>{_CN_NUM})\s*分钟?)?"
    rf"|(?P<only_minutes>{_CN_NUM})\s*分钟"
    rf")\s*(?:之|以)?后"
)
_CN_DAYS_LATER = re.compile(rf"({_CN_NUM})\s*天(?:之|以)?后")
_CN_DATE = re.compile(
    rf"(?:(\d{{4}})\s*年\s*)?({_CN_NUM})\s*月\s*({_CN_NUM})\s*[日号號]"
)
_CN_NEXT_MONTH_DAY = re.compile(rf"下\s*个?\s*月\s*({_CN_NUM})\s*[日号號]")
_CN_WEEKDAY = re.compile(r"(这|本|下下|下)?(周|星期|礼拜)([一二三四五六日天1-7])")
_CN_WEEKDAY_MAP = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6,
}
_WEEKDAY_WEEK_OFFSETS = {"这": 0, "本": 0, "下": 7, "下下": 14}
_DAY_WORD_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_CN_AT = re.compile(
    rf"(大后天|今天|明天|后天)?\s*(早上|早晨|上午|中午|下午|傍晚|晚上|今晚)?\s*({_CN_NUM})\s*[点點时:：]"
    rf"\s*(?:(半)|({_CN_NUM})\s*分?)?"
)
_CN_BARE_HHMM = re.compile(r"(?<!\d)([01]\d|2[0-3])[:：]([0-5]\d)(?!\d)(?![:：]\d)")
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


def _parse_cn_time(match: re.Match[str]) -> tuple[int, int] | None:
    """Extract (hour, minute) from a _CN_AT match and apply period rules."""
    hour = _cn_int(match.group(3))
    minute = 30 if match.group(4) else (_cn_int(match.group(5)) if match.group(5) else 0)
    if hour is None or minute is None or hour > 23 or minute > 59:
        return None
    period = match.group(2) or ""
    if period in ("下午", "傍晚", "晚上", "今晚") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 6:
        hour += 12
    return hour, minute


def _extract_following_cn_time(
    text: str, pos: int
) -> tuple[re.Match[str] | None, int, int] | None:
    """Parse an optional _CN_AT time after pos; return None if invalid or ambiguous."""
    time_m = _CN_AT.search(text, pos)
    if time_m:
        if time_m.group(1):
            return None
        if not (
            time_m.group(2)
            or text[time_m.end(3) : time_m.end(3) + 1] in "点點时"
            or time_m.group(5)
        ):
            time_m = None

    if not time_m:
        return None, 9, 0
    parsed_time = _parse_cn_time(time_m)
    if parsed_time is None:
        return None
    hour, minute = parsed_time
    return time_m, hour, minute


def _strip_anchor_and_time(
    text: str, anchor_m: re.Match[str], time_m: re.Match[str] | None
) -> str:
    """Strip an anchor match and optional following time match from text."""
    if time_m is None:
        return _strip_match(text, anchor_m)
    if not text[anchor_m.end() : time_m.start()].strip():
        return _strip_span(text, anchor_m.start(), time_m.end())
    parts = (
        text[: anchor_m.start()].strip(),
        text[anchor_m.end() : time_m.start()].strip(),
        text[time_m.end() :].strip(),
    )
    return " ".join(p for p in parts if p)


def _parse_cn_date(
    text: str, date_m: re.Match[str], now_local: datetime
) -> tuple[str, datetime] | None:
    """Parse an explicit Chinese date with an optional following time."""
    year_raw = date_m.group(1)
    month = _cn_int(date_m.group(2))
    day = _cn_int(date_m.group(3))
    if month is None or day is None or not (1 <= month <= 12):
        return None
    year = int(year_raw) if year_raw else now_local.year
    try:
        datetime(year, month, day, tzinfo=now_local.tzinfo)
    except ValueError:
        return None

    extracted = _extract_following_cn_time(text, date_m.end())
    if extracted is None:
        return None
    time_m, hour, minute = extracted

    try:
        due = datetime(year, month, day, hour, minute, tzinfo=now_local.tzinfo)
    except ValueError:
        return None
    if year_raw is None:
        if due <= now_local:
            try:
                due = datetime(year + 1, month, day, hour, minute, tzinfo=now_local.tzinfo)
            except ValueError:
                return None
    elif due <= now_local:
        return None

    return _strip_anchor_and_time(text, date_m, time_m), due


def _parse_cn_weekday(
    text: str, weekday_m: re.Match[str], now_local: datetime
) -> tuple[str, datetime] | None:
    """Parse a Chinese weekday phrase with an optional following time."""
    target_wd = _CN_WEEKDAY_MAP[weekday_m.group(3)]
    extracted = _extract_following_cn_time(text, weekday_m.end())
    if extracted is None:
        return None
    time_m, hour, minute = extracted

    today = now_local.date()
    today_wd = today.weekday()
    week_monday = today - timedelta(days=today_wd)
    prefix = weekday_m.group(1) or ""

    if prefix in _WEEKDAY_WEEK_OFFSETS:
        target_date = week_monday + timedelta(
            days=_WEEKDAY_WEEK_OFFSETS[prefix] + target_wd
        )
    else:
        target_date = today + timedelta(days=(target_wd - today_wd) % 7)

    due = datetime(
        target_date.year, target_date.month, target_date.day, hour, minute,
        tzinfo=now_local.tzinfo,
    )
    if due <= now_local:
        if prefix in ("这", "本"):
            return None
        if not prefix:
            target_date += timedelta(days=7)
            due = datetime(
                target_date.year, target_date.month, target_date.day, hour, minute,
                tzinfo=now_local.tzinfo,
            )

    return _strip_anchor_and_time(text, weekday_m, time_m), due


def _parse_cn_in(match: re.Match[str]) -> timedelta | None:
    """Compute a timedelta from a _CN_IN match, or None on invalid numerals."""
    if match.group("only_minutes"):
        minutes = _cn_int(match.group("only_minutes"))
        return None if minutes is None else timedelta(minutes=minutes)

    if match.group("half_only"):
        total_minutes = 30
    else:
        hours = _cn_int(match.group("hours"))
        if hours is None:
            return None
        total_minutes = hours * 60
        if "半" in (match.group("half_hour") or ""):
            total_minutes += 30

    if match.group("minutes"):
        extra = _cn_int(match.group("minutes"))
        if extra is None:
            return None
        total_minutes += extra

    return timedelta(minutes=total_minutes)


def _parse_cn_days_later(
    text: str, days_m: re.Match[str], now_local: datetime
) -> tuple[str, datetime] | None:
    """Parse N天后 / N天之后 with an optional following time."""
    days = _cn_int(days_m.group(1))
    if days is None:
        return None
    extracted = _extract_following_cn_time(text, days_m.end())
    if extracted is None:
        return None
    time_m, hour, minute = extracted
    target_date = (now_local + timedelta(days=days)).date()
    due = datetime(
        target_date.year, target_date.month, target_date.day, hour, minute,
        tzinfo=now_local.tzinfo,
    )
    return _strip_anchor_and_time(text, days_m, time_m), due


def _parse_cn_next_month_day(
    text: str, next_month_m: re.Match[str], now_local: datetime
) -> tuple[str, datetime] | None:
    """Parse 下个月N号/日 (or 下月N号) with an optional following time."""
    day = _cn_int(next_month_m.group(1))
    if day is None:
        return None
    year, month = (now_local.year + 1, 1) if now_local.month == 12 else (now_local.year, now_local.month + 1)
    try:
        datetime(year, month, day, tzinfo=now_local.tzinfo)
    except ValueError:
        return None
    extracted = _extract_following_cn_time(text, next_month_m.end())
    if extracted is None:
        return None
    time_m, hour, minute = extracted
    due = datetime(year, month, day, hour, minute, tzinfo=now_local.tzinfo)
    return _strip_anchor_and_time(text, next_month_m, time_m), due


def _parse_chinese(text: str, now_local: datetime) -> tuple[str, datetime] | None:
    """明天早上8点 / 下午3点半 / 今晚九点十五分 / 10分钟后 / 2小时后 / 10:30."""
    m = _CN_IN.search(text)
    if m:
        delta = _parse_cn_in(m)
        return None if delta is None else (_strip_match(text, m), now_local + delta)
    days_m = _CN_DAYS_LATER.search(text)
    if days_m:
        return _parse_cn_days_later(text, days_m, now_local)
    date_m = _CN_DATE.search(text)
    if date_m:
        return _parse_cn_date(text, date_m, now_local)
    next_month_m = _CN_NEXT_MONTH_DAY.search(text)
    if next_month_m:
        return _parse_cn_next_month_day(text, next_month_m, now_local)
    weekday_m = _CN_WEEKDAY.search(text)
    if weekday_m:
        return _parse_cn_weekday(text, weekday_m, now_local)
    m = _CN_AT.search(text)
    if m and (m.group(1) or m.group(2) or text[m.end(3):m.end(3) + 1] in "点點时"):
        parsed_time = _parse_cn_time(m)
        if parsed_time is None:
            return None
        hour, minute = parsed_time
        day = _DAY_WORD_OFFSETS.get(m.group(1) or "", 0)
        base = (now_local + timedelta(days=day)).date()
        due = datetime(base.year, base.month, base.day, hour, minute, tzinfo=now_local.tzinfo)
        if not m.group(1) and (m.group(2) or "") != "今晚" and due <= now_local:
            due += timedelta(days=1)  # "下午3点" said after 3pm means tomorrow
        return _strip_match(text, m), due
    bare_m = _CN_BARE_HHMM.search(text)
    if bare_m:
        hour, minute = int(bare_m.group(1)), int(bare_m.group(2))
        base = now_local.date()
        due = datetime(base.year, base.month, base.day, hour, minute, tzinfo=now_local.tzinfo)
        if due <= now_local:
            due += timedelta(days=1)
        return _strip_match(text, bare_m), due
    return None


REMIND_USAGE = (
    "用法: /remind <内容> [时间]\n"
    "时间格式:\n"
    "  in 10m / in 2h\n"
    "  tomorrow HH:MM\n"
    "  ISO 时间 (2026-06-16T09:00:00)\n"
    "  中文: 10分钟后 / 半小时后 / 2小时后 / 3天后 / 明天早上8点 / 下午3点半 / 今晚九点 / 2027年1月1日上午9点 / 12月25号晚上8点 / 下个月3号 / 下周一上午10点 / 周五下午3点\n"
    "示例:\n"
    "  /remind in 10m 取快递\n"
    "  /remind 站会 tomorrow 09:00\n"
    "  /remind 2026-06-16T09:00:00 提交报告"
)


def parse_reminder_text(
    raw: str,
    *,
    tz_name: str,
    now: datetime | None = None,
) -> tuple[str, datetime] | None:
    """Return (body, due_at) or None if no time token found."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = now.astimezone(tz) if now is not None else datetime.now(tz)

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


def _strip_span(text: str, start: int, end: int) -> str:
    before = text[:start].strip()
    after = text[end:].strip()
    if before and after:
        return f"{before} {after}".strip()
    return (before or after).strip()


def _strip_match(text: str, match: re.Match[str]) -> str:
    return _strip_span(text, match.start(), match.end())
