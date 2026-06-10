"""handlers/tools/confirm.py — pending confirmation store for dangerous tools.

Channel-agnostic: stores enough context to resume execution after
the operator confirms via Telegram inline button or text YES/确认.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

_CONFIRM_TTL_SECONDS = 300.0


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
# operator_id -> token (most recent pending for text YES fallback)
_by_operator: dict[str, str] = {}


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
    _by_operator[operator_id] = token
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
        if _by_operator.get(action.operator_id) == token:
            _by_operator.pop(action.operator_id, None)
    return action


def get_pending_for_operator(operator_id: str) -> PendingToolAction | None:
    token = _by_operator.get(operator_id)
    if not token:
        return None
    return get_pending(token)


def is_confirmation_text(text: str) -> bool:
    body = (text or "").strip().lower()
    return body in ("yes", "y", "确认", "确定", "ok", "好", "是")


def clear_all_pending() -> None:
    """Test helper: drop all pending confirmations."""
    _pending.clear()
    _by_operator.clear()


def is_cancellation_text(text: str) -> bool:
    body = (text or "").strip().lower()
    return body in ("no", "n", "取消", "算了", "否")
