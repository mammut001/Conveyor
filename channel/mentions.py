"""channel/mentions.py — detect / strip the bot's own @mention.

Shared by the Telegram and Feishu adapters so "@bot /status" and
"@bot 这是真的吗" reach the dispatcher as "/status" and "这是真的吗".
No SDK imports.
"""
from __future__ import annotations

import re


def _pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w@])@" + re.escape(name) + r"(?!\w)", re.IGNORECASE)


def mentions(text: str, name: str) -> bool:
    """Whether ``text`` contains an ``@name`` mention."""
    if not text or not name:
        return False
    return bool(_pattern(name).search(text))


def strip_mention(text: str, name: str) -> str:
    """Remove ``@name`` mentions (case-insensitive) and tidy whitespace."""
    if not text or not name:
        return (text or "").strip()
    stripped = _pattern(name).sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", stripped).strip()
