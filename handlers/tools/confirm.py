"""handlers/tools/confirm.py — pending confirmation store for dangerous tools.

Channel-agnostic: stores enough context to resume execution after
the operator confirms via Telegram inline button or text YES/确认.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    store_key: str = ""

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def is_expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) > self.expires_at


_lock = threading.RLock()
_pending: dict[str, PendingToolAction] = {}
_by_context: dict[ContextKey, str] = {}
_store_path: Path | None = None


def configure_confirmation_store(path: Path | str | None) -> None:
    """Shared pending-confirmation file so another process can see waiting work."""
    global _store_path
    _store_path = Path(path) if path else None


def _confirmation_path(settings: Any = None) -> Path | None:
    """Explicit settings win. The process-global path remains the legacy default."""
    if settings is not None:
        root = getattr(settings, "codex_memory_root", None)
        if root:
            return Path(root) / "state" / "job_queue.sqlite3"
    return _store_path


def _persist_conn(path: Path | None) -> sqlite3.Connection | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_tool_confirmations (
               token TEXT PRIMARY KEY,
               tool_name TEXT NOT NULL,
               operator_id TEXT NOT NULL,
               chat_id TEXT NOT NULL,
               channel TEXT NOT NULL,
               created_at REAL NOT NULL,
               expires_at REAL NOT NULL,
               arg TEXT NOT NULL DEFAULT ''
           )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_tool_confirmations)")}
    if "arg" not in columns:
        conn.execute("ALTER TABLE pending_tool_confirmations ADD COLUMN arg TEXT NOT NULL DEFAULT ''")
    return conn


def _action_from_row(row: sqlite3.Row, store_key: str) -> PendingToolAction:
    created = float(row["created_at"])
    expires = float(row["expires_at"])
    return PendingToolAction(
        token=str(row["token"]),
        tool_name=str(row["tool_name"]),
        arg=str(row["arg"] or ""),
        operator_id=str(row["operator_id"]),
        chat_id=str(row["chat_id"]),
        channel=str(row["channel"]),
        created_at=created,
        ttl_seconds=max(0.0, expires - created),
        store_key=store_key,
    )


def _persist_upsert(action: PendingToolAction, settings: Any = None) -> None:
    path = Path(action.store_key) if action.store_key else _confirmation_path(settings)
    conn = _persist_conn(path)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                """INSERT INTO pending_tool_confirmations
                   (token, tool_name, arg, operator_id, chat_id, channel, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(token) DO UPDATE SET
                     expires_at = excluded.expires_at, chat_id = excluded.chat_id, arg = excluded.arg""",
                (
                    action.token, action.tool_name, action.arg, action.operator_id, action.chat_id,
                    action.channel, action.created_at, action.expires_at,
                ),
            )
    finally:
        conn.close()


def _claim_db(token: str, path: Path | None) -> tuple[str, PendingToolAction | None]:
    """Atomically take one row. ``memory`` means there is no database to claim."""
    if path is None or not path.exists():
        return "memory", None
    conn = _persist_conn(path)
    if conn is None:
        return "memory", None
    try:
        with conn:
            row = conn.execute(
                """DELETE FROM pending_tool_confirmations
                   WHERE token = ? AND expires_at > ?
                   RETURNING token, tool_name, arg, operator_id, chat_id, channel, created_at, expires_at""",
                (token, time.time()),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "lost", None
    return "won", _action_from_row(row, str(path))


def shared_pending_contexts(path: Path | str | None = None, *, now: float | None = None) -> set[tuple[str, str, str]]:
    """Live confirmation contexts ``(channel, operator_id, chat_id)`` from the shared file."""
    target = Path(path) if path else _store_path
    if target is None or not target.exists():
        return set()
    stamp = time.time() if now is None else now
    conn = sqlite3.connect(str(target), timeout=10.0)
    try:
        rows = conn.execute(
            """SELECT channel, operator_id, chat_id FROM pending_tool_confirmations
               WHERE expires_at > ?""",
            (stamp,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {(str(row[0]), str(row[1]), str(row[2])) for row in rows}


def _context_key(operator_id: str, chat_id: str, channel: str) -> ContextKey:
    return operator_id, chat_id, channel


def create_pending(
    tool_name: str,
    arg: str,
    operator_id: str,
    chat_id: str,
    channel: str,
    settings: Any = None,
) -> PendingToolAction:
    path = _confirmation_path(settings)
    token = uuid.uuid4().hex[:12]
    action = PendingToolAction(
        token=token,
        tool_name=tool_name,
        arg=arg,
        operator_id=operator_id,
        chat_id=chat_id,
        channel=channel,
        store_key=str(path) if path is not None else "",
    )
    with _lock:
        _pending[token] = action
        _by_context[_context_key(operator_id, chat_id, channel)] = token
    _persist_upsert(action, settings)
    return action


def _forget(token: str) -> None:
    action = _pending.pop(token, None)
    if action is None:
        return
    key = _context_key(action.operator_id, action.chat_id, action.channel)
    if _by_context.get(key) == token:
        _by_context.pop(key, None)


def _remember(action: PendingToolAction) -> None:
    _pending[action.token] = action
    key = _context_key(action.operator_id, action.chat_id, action.channel)
    current = _pending.get(_by_context.get(key, ""))
    if current is None or current.token == action.token or current.created_at <= action.created_at:
        _by_context[key] = action.token


def _load_token(path: Path | None, token: str) -> PendingToolAction | None:
    if path is None or not path.exists():
        return None
    conn = _persist_conn(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM pending_tool_confirmations WHERE token = ? AND expires_at > ?",
            (token, time.time()),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _action_from_row(row, str(path))


def _load_context(path: Path | None, operator_id: str, chat_id: str, channel: str) -> PendingToolAction | None:
    if path is None or not path.exists():
        return None
    conn = _persist_conn(path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """SELECT * FROM pending_tool_confirmations
               WHERE operator_id = ? AND chat_id = ? AND channel = ? AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (operator_id, chat_id, channel, time.time()),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return _action_from_row(row, str(path))


def _load_pending_row(token: str, settings: Any = None) -> PendingToolAction | None:
    return _load_token(_confirmation_path(settings), token)


def get_pending(token: str, settings: Any = None) -> PendingToolAction | None:
    """Return a live action. A settings root trusts its database, not a stale cache.

    Memory-only actions (no ``store_key``) stay visible to callers that omit
    settings. They are invisible to a different settings root. A token that was
    persisted is gone once its row is gone, so a claimed token cannot be
    revived from this process's cache.
    """
    if settings is not None:
        path = _confirmation_path(settings)
        if path is None:
            return None
        loaded = _load_token(path, token)
        with _lock:
            cached = _pending.get(token)
            if loaded is None:
                if cached is not None and cached.store_key == str(path):
                    _forget(token)
                return None
            _remember(loaded)
            return loaded
    with _lock:
        cached = _pending.get(token)
    if cached is not None and cached.is_expired():
        pop_pending(token)
        return None
    if cached is not None and cached.store_key:
        loaded = _load_token(Path(cached.store_key), token)
        with _lock:
            if loaded is None:
                _forget(token)
                return None
            _remember(loaded)
            return loaded
    if cached is not None:
        return cached
    if _store_path is not None:
        loaded = _load_token(_store_path, token)
        if loaded is None:
            return None
        with _lock:
            _remember(loaded)
        return loaded
    return None


def replace_pending_arg(token: str, new_arg: str, settings: Any = None) -> PendingToolAction | None:
    """Atomically replace the arg of an unexpired pending action.

    Keeps token, tool_name, channel, chat_id, operator_id, created_at,
    and ttl_seconds unchanged. Returns None if not found or expired.
    Writes the new arg through ``store_key`` so another process runs it.
    A claimed row is not inserted again.
    """
    action = get_pending(token, settings=settings) if settings is not None else get_pending(token)
    if action is None:
        return None
    if settings is not None:
        expected = _confirmation_path(settings)
        if expected is None or action.store_key != str(expected):
            return None
    if action.store_key and _load_token(Path(action.store_key), token) is None:
        with _lock:
            _forget(token)
        return None
    action.arg = new_arg
    if action.store_key:
        _persist_upsert(action)
    return action


def set_pending_ttl(token: str, ttl_seconds: float, settings: Any = None) -> PendingToolAction | None:
    """Extend/shorten the lifetime of a live pending action (e.g. routine approvals)."""
    action = get_pending(token, settings=settings) if settings is not None else get_pending(token)
    if action is None:
        return None
    if action.store_key and _load_token(Path(action.store_key), token) is None:
        with _lock:
            _forget(token)
        return None
    action.ttl_seconds = float(ttl_seconds)
    if action.store_key or settings is not None:
        _persist_upsert(action, settings)
    return action


def restore_pending(action: PendingToolAction, settings: Any = None) -> bool:
    """Re-insert a persisted pending action after a restart. Returns False if expired.

    Does not claim the per-context slot if a newer action already holds it.
    ``settings`` pins the row to that root instead of the process-global path.
    """
    if settings is not None:
        path = _confirmation_path(settings)
        if path is not None:
            action.store_key = str(path)
    elif not action.store_key and _store_path is not None:
        action.store_key = str(_store_path)
    with _lock:
        if action.is_expired():
            return False
        _remember(action)
    _persist_upsert(action, settings)
    return True


def pop_pending(token: str, settings: Any = None) -> PendingToolAction | None:
    """Claim one confirmation. The database delete is the cross-process lock."""
    with _lock:
        action = _pending.get(token)
    store_key = action.store_key if action is not None else ""
    expected = _confirmation_path(settings) if settings is not None else None
    if settings is not None and not store_key and action is not None:
        return None
    if settings is not None and store_key and expected is not None and store_key != str(expected):
        return None
    if store_key:
        path: Path | None = Path(store_key)
    elif settings is not None:
        path = expected
    else:
        path = _store_path
    if path is not None and not path.exists() and store_key:
        with _lock:
            _forget(token)
        return None
    status, claimed = _claim_db(token, path)
    with _lock:
        cached = _pending.pop(token, None)
        holder = cached or action or claimed
        if holder is not None:
            key = _context_key(holder.operator_id, holder.chat_id, holder.channel)
            if _by_context.get(key) == token:
                _by_context.pop(key, None)
    if status == "lost":
        return None
    if status == "won":
        return claimed or cached
    if settings is not None and (cached is None or not cached.store_key):
        return None
    return cached


def get_pending_for_context(
    operator_id: str,
    chat_id: str,
    channel: str,
    settings: Any = None,
) -> PendingToolAction | None:
    if settings is not None:
        loaded = _load_context(_confirmation_path(settings), operator_id, chat_id, channel)
        if loaded is None:
            return None
        with _lock:
            _remember(loaded)
        return loaded
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


def list_pending(channel: str | None = None, settings: Any = None) -> list[PendingToolAction]:
    """Return all non-expired pending tool actions, optionally filtered by channel.

    When ``settings`` is set, rows from other memory roots are left out.
    In-process actions that were not written to a root stay visible so older
    callers that omit settings keep working.
    """
    path = _confirmation_path(settings) if settings is not None else _store_path
    path_key = str(path) if path is not None else ""
    with _lock:
        now = time.time()
        for token, action in list(_pending.items()):
            if action.is_expired(now):
                _forget(token)
        actions = []
        for action in _pending.values():
            if settings is not None:
                if not path_key or action.store_key != path_key:
                    continue
            elif action.store_key and path_key and action.store_key != path_key:
                continue
            actions.append(action)
        seen = {action.token for action in actions}
    live_tokens: set[str] = set()
    if path is not None and path.exists():
        conn = _persist_conn(path)
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT * FROM pending_tool_confirmations WHERE expires_at > ?",
                    (time.time(),),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()
            for row in rows:
                token = str(row["token"])
                live_tokens.add(token)
                if token in seen:
                    continue
                actions.append(_action_from_row(row, path_key))
        actions = [action for action in actions if not action.store_key or action.token in live_tokens]
    elif settings is not None:
        actions = []
    else:
        confirmed = []
        for action in actions:
            if not action.store_key:
                confirmed.append(action)
                continue
            if _load_token(Path(action.store_key), action.token) is None:
                with _lock:
                    _forget(action.token)
                continue
            confirmed.append(action)
        actions = confirmed
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
