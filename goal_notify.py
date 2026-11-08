"""goal_notify.py — 目标通知：告诉老板「这个目标需要你」或者「这个目标结束了」。

A goal used to reach `waiting_user` / `achieved` / `exhausted` / `failed` and then
just sit on the 项目 page until the owner happened to look. This module sends one
short Simplified-Chinese message on the channels Conveyor already has:

- **Web Push** (kind ``goal``): only the opaque goal id and a generic Chinese
  title. The goal title / question / reason never travel with it, because push
  payloads are stored and rendered by the browser vendor's servers.
- **Telegram / Feishu**: the full Chinese text, through the same owner-delivery
  helpers the sentry alerts use (`personal_tools.sentry.send_owner_message`).

Delivery is non-blocking (own bounded queue + one daemon thread, exactly like
`web_push`), never raises, and never logs a message body. Every notification is
recorded in the goals database keyed by `(goal_id, state, updated_at)`, so a
restart or a second loop tick never tells the owner the same thing twice.
`stopped` is deliberately absent: the owner stopped the goal themselves.
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("conveyor.goal_notify")

# Web Push kind for goals; `web_push` owns the generic Chinese title it maps to
# (`web_push.KIND_TITLES`), because that is where the payload is built.
PUSH_KIND = "goal"
# Only these transitions are worth interrupting the owner for. `stopped` is not:
# the owner did that themselves.
NOTIFY_STATES = ("waiting_user", "achieved", "exhausted", "failed")

OPEN_HINT = "打开 Conveyor 的「项目」页查看。"

# How much of the owner-facing question / stop_reason is repeated in the message.
MAX_DETAIL_CHARS = 200
# Bound retained dedupe rows per goals DB (each goal only ever needs a handful).
MAX_DEDUPE_ROWS = 500
QUEUE_MAXSIZE = 128

_QUEUE: "queue.Queue[tuple[Any, dict, str]]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


# ---------------------------------------------------------------------------
# Text (Simplified Chinese only — the owner is not a developer)
# ---------------------------------------------------------------------------

def enabled(settings: object) -> bool:
    """Off switch: ``CONVEYOR_GOAL_NOTIFY=false`` (`Settings.goal_notify_enabled`)."""
    return bool(getattr(settings, "goal_notify_enabled", True))


def _clip(text: object) -> str:
    """One line, at most `MAX_DETAIL_CHARS` characters, ending in a Chinese ellipsis."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > MAX_DETAIL_CHARS:
        return collapsed[:MAX_DETAIL_CHARS] + "…"
    return collapsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _goal_id(goal: dict) -> str:
    return str(goal.get("id") or "").strip()


def _stored_goal(settings: Any, goal: dict) -> dict:
    """Fresh goal row from the goals DB; `{}` when it cannot be read."""
    goal_id = _goal_id(goal)
    if not goal_id or settings is None:
        return {}
    try:
        from supervisor_goals import _get_goal_row
        return _get_goal_row(settings, goal_id)
    except Exception:
        return {}


def _goal_int(settings: Any, goal: dict, key: str) -> int:
    """Positive integer field of the goal, falling back to the stored row."""
    for source in (goal, _stored_goal(settings, goal)):
        raw = source.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            return raw
        if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
            return int(raw.strip())
    return 0


def _rounds(settings: Any, goal: dict) -> int:
    """How many iterations the goal has used."""
    used = goal.get("iterations_used")
    if isinstance(used, int) and used > 0:
        return used
    iterations = goal.get("iterations")
    if isinstance(iterations, list) and iterations:
        return len(iterations)
    try:
        from supervisor_goals import _iterations
        return len(_iterations(settings, _goal_id(goal)))
    except Exception:
        return 0


def notification_text(settings: Any, goal: dict, new_state: str) -> str:
    """The Simplified-Chinese message for one goal transition ('' when not notifiable)."""
    if not isinstance(goal, dict):
        return ""
    state = str(new_state or "")
    if state not in NOTIFY_STATES:
        return ""
    title = " ".join(str(goal.get("title") or "").split()) or "未命名目标"
    if state == "waiting_user":
        detail = _clip(goal.get("question")) or "请在「项目」里看看这个目标，然后告诉我下一步。"
        body = f"目标「{title}」需要你回复：{detail}"
    elif state == "achieved":
        rounds = _rounds(settings, goal)
        body = f"目标「{title}」已达成（第 {rounds} 轮）。" if rounds else f"目标「{title}」已达成。"
    elif state == "exhausted":
        budget = _goal_int(settings, goal, "max_iterations")
        body = f"目标「{title}」用完了 {budget} 轮还没达成。" if budget else f"目标「{title}」的迭代次数用完了，还没达成。"
    else:  # failed
        reason = _clip(goal.get("stop_reason")) or "具体原因没有记录下来。"
        body = f"目标「{title}」没能完成：{reason}"
    return f"{body}\n\n{OPEN_HINT}"


# ---------------------------------------------------------------------------
# Dedupe: one row per (goal, state, updated_at) inside the goals database
# ---------------------------------------------------------------------------

_NOTIFICATIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_goal_notifications (
    goal_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, state, updated_at)
)
"""


def _claim(settings: Any, goal_id: str, state: str, updated_at: str) -> bool:
    """Atomically claim the right to notify. False when it was already claimed.

    Lives in the goals DB, so a restart cannot re-send. A storage failure also
    returns False: staying quiet beats repeating a message the owner already read.
    """
    if settings is None or not goal_id or not state or not updated_at:
        return False
    try:
        from supervisor_goals import _connect
        conn = _connect(settings)
    except Exception:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_NOTIFICATIONS_SCHEMA_SQL)
        cursor = conn.execute(
            """INSERT OR IGNORE INTO supervisor_goal_notifications
               (goal_id, state, updated_at, sent_at) VALUES (?, ?, ?, ?)""",
            (goal_id, state, updated_at, _utc_now()),
        )
        claimed = cursor.rowcount == 1
        if claimed:
            # Keep the bookkeeping table small without ever losing a live claim.
            conn.execute(
                """DELETE FROM supervisor_goal_notifications WHERE rowid NOT IN
                   (SELECT rowid FROM supervisor_goal_notifications ORDER BY rowid DESC LIMIT ?)""",
                (MAX_DEDUPE_ROWS,),
            )
        conn.commit()
        return claimed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def _deliver_push(settings: Any, goal_id: str, text: str) -> None:
    """Web Push: generic Chinese title + goal id only (see the module docstring)."""
    import web_push
    web_push.queue_notification(settings, goal_id, PUSH_KIND)


def _deliver_telegram(settings: Any, goal_id: str, text: str) -> None:
    """Telegram: the full Chinese text, through the shared sentry delivery helper."""
    from personal_tools.sentry import send_owner_telegram
    send_owner_telegram(settings, text)


def _deliver_feishu(settings: Any, goal_id: str, text: str) -> None:
    """Feishu: the full Chinese text, through the shared sentry delivery helper."""
    from personal_tools.sentry import send_owner_feishu
    send_owner_feishu(settings, text)


# Resolved by name on every call so a patched/replaced channel is honoured.
_CHANNELS = ("_deliver_push", "_deliver_telegram", "_deliver_feishu")


def _fan_out(settings: Any, goal_id: str, text: str) -> None:
    for name in _CHANNELS:
        channel = globals().get(name)
        if channel is None:  # pragma: no cover - defensive
            continue
        try:
            channel(settings, goal_id, text)
        except Exception:
            # One broken channel must never stop the others, kill the worker, or
            # print a transport error that may embed secrets.
            log.debug("goal %s: notification channel %s failed", goal_id, name)


def _deliver(settings: Any, goal: dict, state: str) -> None:
    if not enabled(settings) or state not in NOTIFY_STATES:
        return
    goal_id = _goal_id(goal)
    if not _claim(settings, goal_id, state, str(goal.get("updated_at") or "")):
        return
    text = notification_text(settings, goal, state)
    if not text:
        return
    _fan_out(settings, goal_id, text)
    log.info("goal %s -> %s: owner notified", goal_id, state)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            threading.Thread(target=_worker, name="conveyor-goal-notify", daemon=True).start()
            _WORKER_STARTED = True


def notify_goal_change(settings: Any, goal: dict, new_state: str) -> None:
    """Queue one owner notification for a goal transition. Never blocks, never raises.

    Called from the goal loop on every transition into `NOTIFY_STATES`; the
    dedupe claim inside the worker makes a second tick (or a restart) a no-op.
    """
    if settings is None or not isinstance(goal, dict):
        return
    state = str(new_state or "")
    if state not in NOTIFY_STATES or not enabled(settings) or not _goal_id(goal):
        return
    _ensure_worker()
    try:
        _QUEUE.put_nowait((settings, dict(goal), state))
    except queue.Full:
        # The owner would rather miss a notification than have the goal loop stall.
        pass


def _worker() -> None:
    while True:
        settings, goal, state = _QUEUE.get()
        try:
            _deliver(settings, goal, state)
        except Exception:
            # Owner notifications must never interrupt goal processing.
            pass
        finally:
            _QUEUE.task_done()
