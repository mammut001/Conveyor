"""handlers/tools/confirm.py — pending confirmation store for dangerous tools.

Channel-agnostic: stores enough context to resume execution after
the operator confirms via Telegram inline button or text YES/确认.
"""
from __future__ import annotations

import threading
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
    ttl_seconds: float = _CONFIRM_TTL_SECONDS

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def is_expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) > self.expires_at


_lock = threading.RLock()
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
    with _lock:
        _pending[token] = action
        _by_context[_context_key(operator_id, chat_id, channel)] = token
    return action


def get_pending(token: str) -> PendingToolAction | None:
    with _lock:
        action = _pending.get(token)
        if action is None:
            return None
        if action.is_expired():
            pop_pending(token)
            return None
        return action


def replace_pending_arg(token: str, new_arg: str) -> PendingToolAction | None:
    """Atomically replace the arg of an unexpired pending action.

    Keeps token, tool_name, channel, chat_id, operator_id, created_at,
    and ttl_seconds unchanged. Returns None if not found or expired.
    """
    with _lock:
        action = get_pending(token)
        if action is None:
            return None
        action.arg = new_arg
        return action


def set_pending_ttl(token: str, ttl_seconds: float) -> PendingToolAction | None:
    """Extend/shorten the lifetime of a live pending action (e.g. routine approvals)."""
    with _lock:
        action = get_pending(token)
        if action is not None:
            action.ttl_seconds = float(ttl_seconds)
        return action


def restore_pending(action: PendingToolAction) -> bool:
    """Re-insert a persisted pending action after a restart. Returns False if expired.

    Does not claim the per-context slot if a newer action already holds it.
    """
    with _lock:
        if action.is_expired():
            return False
        _pending[action.token] = action
        key = _context_key(action.operator_id, action.chat_id, action.channel)
        current = _pending.get(_by_context.get(key, ""))
        if current is None or current.created_at <= action.created_at:
            _by_context[key] = action.token
        return True


def pop_pending(token: str) -> PendingToolAction | None:
    with _lock:
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
    with _lock:
        token = _by_context.get(_context_key(operator_id, chat_id, channel))
        if not token:
            return None
        return get_pending(token)


def get_pending_for_operator(operator_id: str) -> PendingToolAction | None:
    """Backward-compatible helper; prefer get_pending_for_context."""
    with _lock:
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


def list_pending(channel: str | None = None) -> list[PendingToolAction]:
    """Return all non-expired pending tool actions, optionally filtered by channel."""
    with _lock:
        now = time.time()
        for token, action in list(_pending.items()):
            if action.is_expired(now):
                pop_pending(token)
        actions = list(_pending.values())
        if channel is not None:
            actions = [a for a in actions if a.channel == channel]
        actions.sort(key=lambda a: a.created_at, reverse=True)
        return actions


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
    with _lock:
        _pending.clear()
        _by_context.clear()


def is_cancellation_text(text: str) -> bool:
    body = (text or "").strip().lower()
    return body in ("no", "n", "取消", "算了", "否")
