#!/usr/bin/env python3
"""Regression smoke for PR #35 merge blockers.

Covers:
1. /forget clears both the legacy session file and persistent chat-tier history.
2. Unsupported Feishu topic watches are rejected without creating a dead subscription.
"""
from __future__ import annotations

import asyncio
import dataclasses
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel.types import InboundMessage
from config import Settings, load_settings
from handlers import chat
from handlers.dispatch import dispatch
from handlers.session import session_path
from personal_tools.store import PersonalToolsStore
from personal_tools.topic_watch import watch_topic

TMP = Path(tempfile.mkdtemp(prefix="conveyor-pr35-regression-smoke-"))


def _settings(**kw) -> Settings:
    base = dataclasses.replace(
        load_settings(),
        telegram_allowed_user_id=1,
        codex_workspace_root=TMP / "ws",
        codex_task_root=TMP / "tasks",
        codex_memory_root=TMP / "mem",
    )
    return dataclasses.replace(base, **kw)


class _Port:
    supports_inline_buttons = False
    supports_attachments = False

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, _msg, text):
        self.replies.append(text)
        return "1"

    async def send_new(self, _msg, text):
        self.replies.append(text)
        return "1"

    async def edit_progress(self, _msg, _placeholder_id, _text):
        return True

    async def reply_with_buttons(self, _msg, text, _buttons):
        self.replies.append(text)
        return "1"

    async def fetch_attachment(self, _msg, _attachment):
        return None


async def _exercise_forget() -> None:
    settings = _settings()
    msg = InboundMessage(
        channel="telegram",
        operator_id="1",
        chat_id="forget-regression",
        message_id="1",
        text="/forget",
        chat_type="p2p",
    )

    legacy = session_path(settings, msg)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('{"user":"old","assistant":"context"}\n', encoding="utf-8")

    key = chat.chat_key(msg)
    chat.remember(key, "old question", "old answer", 4, settings=settings)
    assert legacy.exists(), "precondition: legacy session file missing"
    assert chat.history(key, 4, settings=settings), "precondition: chat history missing"

    port = _Port()
    await dispatch(msg, port, settings, object())

    assert not legacy.exists(), "/forget must remove the legacy session file"
    assert chat.history(key, 4, settings=settings) == [], "/forget must remove chat-tier history"
    assert any("已清除" in text for text in port.replies), "expected /forget confirmation reply"
    print("[ok] /forget clears legacy session and persistent chat history")


def test_feishu_watch_rejected_without_persisting() -> None:
    settings = _settings()
    result = watch_topic(settings, "ou_operator", "feishu", "oc_chat", "AI agents", 4.0)
    assert result.ok is False, "Feishu watch must be rejected until delivery is implemented"
    assert "没有创建订阅" in result.text

    watches = PersonalToolsStore(settings).list_topic_watches("ou_operator")
    assert watches == [], "rejected Feishu watch must not create a dead subscription"
    print("[ok] unsupported Feishu watch is rejected without persistence")


def main() -> None:
    try:
        asyncio.run(_exercise_forget())
        test_feishu_watch_rejected_without_persisting()
        print("pr35 regression smoke ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
