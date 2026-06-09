"""handlers/memo.py — memo fast path, channel-agnostic.

Replicates the contract of bot.py::_handle_memo_fast_path:
1. Strip the leading memory keyword ("记 x", "记住 x", "/memo x").
2. Optionally parse a leading [category] tag.
3. Classify via runner.classify_memo when no tag is provided.
4. Append to today's MEMORY.md via runner.append_memo.

Telegram and Feishu both call handle_memo(); the OutboundPort routes
the reply.
"""
from __future__ import annotations

import re

from channel.types import InboundMessage, OutboundPort
from redaction import truncate
from runner import CodexRunner

MEMORY_KEYWORD_PATTERN = re.compile(r"^\s*(memo|备忘|记下|记一下|记住|记录|记\b)", re.IGNORECASE)
CATEGORY_PATTERN = re.compile(r"\[(preference|fact|tool-quirk|convention)\]", re.IGNORECASE)

USAGE = "Usage: 记 <内容>  或  /memo <内容>"


def detect_memory_intent(text: str) -> bool:
    return bool(MEMORY_KEYWORD_PATTERN.match(text))


def _strip_keyword(text: str) -> str:
    return MEMORY_KEYWORD_PATTERN.sub("", text, count=1).strip()


async def handle_memo(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    text: str | None = None,
) -> None:
    raw = text if text is not None else msg.text
    stripped = _strip_keyword(raw)
    if not stripped:
        await port.reply(msg, USAGE)
        return

    matches = CATEGORY_PATTERN.findall(stripped)
    if matches:
        category = matches[0].lower()
        content = CATEGORY_PATTERN.sub("", stripped).strip()
    else:
        content = stripped
        if not content:
            await port.reply(msg, USAGE)
            return
        try:
            category = await runner.classify_memo(content)
        except Exception:
            category = "unfiled"

    auto_ts = category == "fact"
    try:
        summary = await runner.append_memo(category, content, auto_timestamp=auto_ts)
    except Exception as exc:
        await port.reply(msg, f"记下来的时候出了点问题：{truncate(str(exc), 1200)}")
        return
    await port.reply(msg, summary)
