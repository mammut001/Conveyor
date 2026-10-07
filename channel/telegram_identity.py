"""Telegram conversation addresses, distinct from Bot API delivery targets.

A plain chat keeps its legacy ID. Forum topics add ``:topic:<id>``; a
selected agent adds ``:agent:<id>``. Persisting this complete address pins
jobs, approvals and history even when the chat selects another agent later.
Only transport adapters decode it into chat_id/message_thread_id.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_ADDRESS = re.compile(r"(?P<chat>-?\d+)(?::topic:(?P<topic>[1-9]\d*))?(?::agent:(?P<agent>[a-z0-9]{1,32}))?\Z")


@dataclass(frozen=True)
class TelegramAddress:
    chat_id: int
    topic_id: int | None = None
    agent_id: str | None = None

    @classmethod
    def parse(cls, value: str | int) -> TelegramAddress:
        match = _ADDRESS.fullmatch(str(value))
        if match is None:
            raise ValueError("Invalid Telegram conversation address")
        return cls(int(match['chat']), int(match['topic']) if match['topic'] else None, match['agent'])

    @property
    def source(self) -> str:
        return str(self.chat_id) + (f":topic:{self.topic_id}" if self.topic_id is not None else "")

    @property
    def conversation(self) -> str:
        return self.source + (f":agent:{self.agent_id}" if self.agent_id else "")

    def destination(self) -> dict:
        result = {"chat_id": self.chat_id}
        if self.topic_id is not None:
            result["message_thread_id"] = self.topic_id
        return result


def source_address(chat_id: str) -> str:
    return TelegramAddress.parse(chat_id).source


def destination(chat_id: str | int) -> dict:
    return TelegramAddress.parse(chat_id).destination()


def context_tag(chat_id: str) -> str:
    """Compact callback binding; no operator data or paths in callback payloads."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]
