"""handlers/tools/confirm.py — pending confirmation store for dangerous tools.

Channel-agnostic: stores enough context to resume execution after
the operator confirms via Telegram inline button or text YES/确认.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

_CONFIRM_TTL_SECONDS = 300.0

ContextKey = tuple[str, str, str]  # operator_id, chat_id, channel


@dataclass
class PendingToolAction:
    token: str
    tool_name: str
    arg: str
    operator_id: str
    chat_id: str
    channel: str
    created_at: float = field(default_factory=time.time)


_pending: dict[str, PendingToolAction] = {}
_by_context: dict[ContextKey, str] = {}


def _context_key(operator_id: str, chat_id: str, channel: str) -> ContextKey:
    return operator_id, chat_id, channel


def create_pending(
    tool_name: str,
    arg: str,
    operator_id: str,
    chat_id: str,
    channel: str,
) -> PendingToolAction:
    token = uuid.uuid4().hex[:12]
    action = PendingToolAction(
        token=token,
        tool_name=tool_name,
        arg=arg,
        operator_id=operator_id,
        chat_id=chat_id,
        channel=channel,
    )
    _pending[token] = action
    _by_context[_context_key(operator_id, chat_id, channel)] = token
    return action


def get_pending(token: str) -> PendingToolAction | None:
    action = _pending.get(token)
    if action is None:
        return None
    if time.time() - action.created_at > _CONFIRM_TTL_SECONDS:
        pop_pending(token)
        return None
    return action


def pop_pending(token: str) -> PendingToolAction | None:
    action = _pending.pop(token, None)
    if action is not None:
        key = _context_key(action.operator_id, action.chat_id, action.channel)
        if _by_context.get(key) == token:
            _by_context.pop(key, None)
    return action


def get_pending_for_context(
    operator_id: str,
    chat_id: str,
    channel: str,
) -> PendingToolAction | None:
    token = _by_context.get(_context_key(operator_id, chat_id, channel))
    if not token:
        return None
    return get_pending(token)


def get_pending_for_operator(operator_id: str) -> PendingToolAction | None:
    """Backward-compatible helper; prefer get_pending_for_context."""
    for key, token in list(_by_context.items()):
        if key[0] == operator_id:
            action = get_pending(token)
            if action is not None:
                return action
    return None


def matches_context(action: PendingToolAction, operator_id: str, chat_id: str, channel: str) -> bool:
    return (
        action.operator_id == operator_id
        and action.chat_id == chat_id
        and action.channel == channel
    )


def is_confirmation_text(text: str) -> bool:
    body = (text or "").strip().lower()
    explicit = (
        "确认",
        "确认执行",
        "确认重启",
        "yes confirm",
        "confirm",
        "execute",
    )
    return body in explicit


def clear_all_pending() -> None:
    """Test helper: drop all pending confirmations."""
    _pending.clear()
    _by_context.clear()


def is_cancellation_text(text: str) -> bool:
    body = (text or "").strip().lower()
    return body in ("no", "n", "取消", "算了", "否")
