"""approval_relay.py — Shared cross-channel approval relay (roadmap P2-2).

Enables a single shared approval store across Web Console, Telegram, and Feishu.
First decision wins across all surfaces; execution happens in the owning process.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Coroutine, Protocol
import urllib.parse
import urllib.request
import uuid

from channel.types import InboundMessage, OutboundPort
from handlers.tools.audit import audit_tool_event
from handlers.tools.confirm import (
    PendingToolAction,
    get_pending,
    list_pending as confirm_list_pending,
)
from redaction import redact_text, truncate

logger = logging.getLogger("conveyor.approval_relay")

INSTANCE_ID: str = uuid.uuid4().hex[:12]
PID: int = os.getpid()

_HOUSEKEEPING_INTERVAL_SECONDS = 3600.0
_last_housekeeping: float = 0.0
_housekeeping_lock = threading.Lock()
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()

# Real notifiers do blocking HTTPS calls; run them on one background worker
# thread so they never stall the bot / web console event loops. When a test
# notifier factory is installed, notifications are dispatched synchronously.
_notify_executor: Any = None
_notify_executor_lock = threading.Lock()


def _dispatch_notification(fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    if _notifier_factory is not None:
        fn(*args, **kwargs)
        return
    global _notify_executor
    with _notify_executor_lock:
        if _notify_executor is None:
            from concurrent.futures import ThreadPoolExecutor
            _notify_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="approval-relay-notify")
    _notify_executor.submit(fn, *args, **kwargs)


def is_relay_enabled(settings: Any) -> bool:
    """Return True only if both approval_relay and approval_inbox are enabled."""
    if settings is None:
        return False
    return bool(
        getattr(settings, "approval_relay_enabled", False)
        and getattr(settings, "approval_inbox_enabled", False)
    )


def db_path(settings: Any) -> Path:
    """Return the Path to the shared relay database."""
    configured = getattr(settings, "approval_relay_db", None)
    if configured:
        return Path(configured)
    memory_root = getattr(settings, "codex_memory_root", Path("~/.codex"))
    return Path(memory_root) / "approval_relay.db"


def _connect(settings: Any) -> sqlite3.Connection:
    """Connect to SQLite database with WAL and 0600 permissions."""
    path = db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()

    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    key = str(path)
    if key in _schema_ready and not is_new:
        return conn
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    with _schema_lock, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relay_approvals (
                token TEXT PRIMARY KEY,
                origin_channel TEXT NOT NULL,
                origin_pid INTEGER NOT NULL,
                origin_instance TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                summary TEXT NOT NULL,
                arg_preview TEXT NOT NULL,
                danger TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                decided_via TEXT,
                decided_by TEXT,
                decided_at REAL,
                claimed_at REAL,
                result_preview TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relay_notifications (
                token TEXT NOT NULL,
                channel TEXT NOT NULL,
                target TEXT NOT NULL,
                external_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (token, channel, external_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_approvals_status_expires ON relay_approvals(status, expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_approvals_claimed ON relay_approvals(claimed_at, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relay_notifications_token ON relay_notifications(token)"
        )

    # Throttled housekeeping (prune > 7 days)
    global _last_housekeeping
    now = time.time()
    if now - _last_housekeeping > _HOUSEKEEPING_INTERVAL_SECONDS:
        with _housekeeping_lock:
            if now - _last_housekeeping > _HOUSEKEEPING_INTERVAL_SECONDS:
                _last_housekeeping = now
                try:
                    cutoff = now - 7 * 86_400
                    with conn:
                        conn.execute("DELETE FROM relay_approvals WHERE created_at < ?", (cutoff,))
                        conn.execute("DELETE FROM relay_notifications WHERE created_at < ?", (cutoff,))
                except Exception:
                    logger.debug("Housekeeping pruning failed", exc_info=True)

    _schema_ready.add(key)
    return conn


# -----------------------------------------------------------------------------
# Rate Limiting for Notifications
# -----------------------------------------------------------------------------

class NotificationRateLimiter:
    """Sliding-window limiter: at most max_count notifications per window_seconds."""

    def __init__(self, max_count: int = 30, window_seconds: float = 600.0) -> None:
        self.max_count = max_count
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._timestamps: dict[str, list[float]] = {}

    def allow(self, channel: str) -> bool:
        now = time.time()
        with self._lock:
            history = self._timestamps.setdefault(channel, [])
            cutoff = now - self.window_seconds
            history[:] = [t for t in history if t > cutoff]
            if len(history) >= self.max_count:
                return False
            history.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


_rate_limiter = NotificationRateLimiter()


# -----------------------------------------------------------------------------
# Notifier Protocol & Implementations
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalNotificationRequest:
    token: str
    channel: str
    target: str
    tool_name: str
    summary: str
    danger: str
    arg_preview: str  # redacted, <= 500 chars
    source: str
    expires_at: float


class ApprovalNotifier(Protocol):
    channel: str

    def send(self, request: ApprovalNotificationRequest) -> str | None:
        """Send notification message, returning external_id or None."""
        ...

    def update(self, external_id: str, target: str, text: str) -> bool:
        """Update existing notification message, removing buttons. Returns True on success."""
        ...


class TelegramRelayNotifier:
    channel: str = "telegram"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def send(self, request: ApprovalNotificationRequest) -> str | None:
        token = getattr(self.settings, "telegram_bot_token", "")
        if not token:
            return None
        expires_str = datetime.fromtimestamp(request.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"⚠️ 危险操作需确认 [来源: {request.source}]\n\n"
            f"工具: {request.tool_name}\n"
            f"说明: {request.summary}\n"
            f"风险: {request.danger}\n"
        )
        if request.arg_preview.strip():
            text += f"参数: {request.arg_preview[:500]}\n"
        text += f"过期时间: {expires_str}\n\n确认执行？"

        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ 批准", "callback_data": f"relay:approve:{request.token}"},
                {"text": "❌ 拒绝", "callback_data": f"relay:reject:{request.token}"},
            ]]
        }
        data = {
            "chat_id": str(request.target),
            "text": truncate(text, 4000),
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(reply_markup),
        }
        payload = urllib.parse.urlencode(data).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.load(resp)
            if res_data.get("ok"):
                return str(res_data["result"]["message_id"])
        except Exception:
            logger.debug("Telegram sendMessage failed for relay notification", exc_info=True)
        return None

    def update(self, external_id: str, target: str, text: str) -> bool:
        token = getattr(self.settings, "telegram_bot_token", "")
        if not token:
            return False
        data = {
            "chat_id": str(target),
            "message_id": int(external_id),
            "text": truncate(text, 4000),
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps({"inline_keyboard": []}),
        }
        payload = urllib.parse.urlencode(data).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.load(resp)
            return bool(res_data.get("ok"))
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.debug("Telegram editMessageText failed for relay notification", exc_info=True)
            return False


class FeishuRelayNotifier:
    channel: str = "feishu"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def _get_tenant_token(self) -> str | None:
        app_id = getattr(self.settings, "lark_app_id", None)
        app_secret = getattr(self.settings, "lark_app_secret", None)
        if not app_id or not app_secret:
            return None
        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        token_payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        req = urllib.request.Request(token_url, data=token_payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                tok_data = json.load(resp)
            return tok_data.get("tenant_access_token")
        except Exception:
            logger.debug("Failed to get Feishu tenant access token", exc_info=True)
            return None

    def send(self, request: ApprovalNotificationRequest) -> str | None:
        tenant_token = self._get_tenant_token()
        if not tenant_token or not request.target:
            return None
        try:
            expires_str = datetime.fromtimestamp(request.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            body_text = (
                f"**工具**: {request.tool_name}\n"
                f"**说明**: {request.summary}\n"
                f"**风险**: {request.danger}\n"
                f"**参数**: {request.arg_preview[:500]}\n"
                f"**过期时间**: {expires_str}"
            )
            card = {
                "config": {"wide_screen_mode": True, "update_multi": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"⚠️ 危险操作需确认 [来源: {request.source}]"},
                    "template": "orange",
                },
                "elements": [
                    {"tag": "markdown", "content": body_text},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "✅ 批准"},
                                "type": "primary",
                                "value": {"action": "relay_approve", "token": request.token},
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "❌ 拒绝"},
                                "type": "default",
                                "value": {"action": "relay_reject", "token": request.token},
                            },
                        ],
                    },
                ],
            }
            msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
            msg_payload = json.dumps({
                "receive_id": request.target,
                "msg_type": "interactive",
                "content": json.dumps(card),
            }).encode("utf-8")
            msg_req = urllib.request.Request(
                msg_url,
                data=msg_payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tenant_token}"},
                method="POST",
            )
            with urllib.request.urlopen(msg_req, timeout=10) as resp:
                msg_data = json.load(resp)
            if msg_data.get("code") == 0:
                return str(msg_data["data"]["message_id"])
        except Exception:
            logger.debug("Feishu send failed for relay notification", exc_info=True)
        return None

    def update(self, external_id: str, target: str, text: str) -> bool:
        tenant_token = self._get_tenant_token()
        if not tenant_token or not external_id:
            return False
        try:
            card = {
                "config": {"wide_screen_mode": True, "update_multi": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "审批操作结果"},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": truncate(text, 1500)},
                ],
            }
            patch_url = f"https://open.feishu.cn/open-apis/im/v1/messages/{external_id}"
            patch_payload = json.dumps({"content": json.dumps(card)}).encode("utf-8")
            patch_req = urllib.request.Request(
                patch_url,
                data=patch_payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {tenant_token}"},
                method="PATCH",
            )
            with urllib.request.urlopen(patch_req, timeout=10) as resp:
                res_data = json.load(resp)
            return res_data.get("code") == 0
        except Exception:
            logger.debug("Feishu card update failed for relay notification", exc_info=True)
            return False


class FakeNotifier:
    """Test and dev-script notifier that records sends and updates without network."""

    def __init__(self, channel: str, log_file: Path | str | None = None) -> None:
        self.channel = channel
        self.log_file = Path(log_file) if log_file else None
        self.sent: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self._counter = 0

    def send(self, request: ApprovalNotificationRequest) -> str | None:
        self._counter += 1
        ext_id = f"fake-{self.channel}-{self._counter}"
        rec = {
            "token": request.token,
            "channel": self.channel,
            "target": request.target,
            "tool_name": request.tool_name,
            "summary": request.summary,
            "danger": request.danger,
            "arg_preview": request.arg_preview,
            "source": request.source,
            "external_id": ext_id,
            "created_at": time.time(),
        }
        self.sent.append(rec)
        if self.log_file:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ext_id

    def update(self, external_id: str, target: str, text: str) -> bool:
        rec = {
            "external_id": external_id,
            "target": target,
            "text": text,
            "updated_at": time.time(),
        }
        self.updated.append(rec)
        if self.log_file:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True


_notifier_factory: Callable[[str, Any], ApprovalNotifier | None] | None = None


def set_notifier_factory(factory: Callable[[str, Any], ApprovalNotifier | None] | None) -> None:
    global _notifier_factory
    _notifier_factory = factory


def reset_notifier_factory() -> None:
    global _notifier_factory
    _notifier_factory = None


def get_notifier(channel: str, settings: Any) -> ApprovalNotifier | None:
    if _notifier_factory is not None:
        return _notifier_factory(channel, settings)
    if channel == "telegram":
        return TelegramRelayNotifier(settings)
    if channel == "feishu":
        return FeishuRelayNotifier(settings)
    return None


def _resolve_target(channel: str, settings: Any) -> str | None:
    if channel == "telegram":
        uid = getattr(settings, "telegram_allowed_user_id", None)
        token = getattr(settings, "telegram_bot_token", None)
        if uid and token:
            return str(uid)
        return None
    elif channel == "feishu":
        app_id = getattr(settings, "lark_app_id", None)
        app_secret = getattr(settings, "lark_app_secret", None)
        open_id = getattr(settings, "lark_allowed_open_id", None)
        if app_id and app_secret and open_id:
            return str(open_id)
        return None
    return None


def _record_notification(
    settings: Any,
    token: str,
    channel: str,
    target: str,
    external_id: str,
) -> None:
    try:
        conn = _connect(settings)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO relay_notifications (
                        token, channel, target, external_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (token, channel, target, external_id, time.time()),
                )
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to record notification for token %s", token, exc_info=True)


def _notify_publish(
    settings: Any,
    action: PendingToolAction,
    *,
    summary: str,
    danger: str,
    source: str,
) -> None:
    channels = getattr(settings, "approval_relay_channels", ()) or ()
    if not channels:
        return
    redacted_arg = truncate(redact_text(action.arg or ""), 500)
    for ch in channels:
        if ch == action.channel:
            continue
        if not _rate_limiter.allow(ch):
            logger.warning("Notification rate limit exceeded for relay channel %s, dropping notification", ch)
            continue
        notifier = get_notifier(ch, settings)
        if notifier is None:
            continue
        target = _resolve_target(ch, settings)
        if not target:
            continue
        req = ApprovalNotificationRequest(
            token=action.token,
            channel=ch,
            target=str(target),
            tool_name=action.tool_name,
            summary=summary or action.tool_name,
            danger=danger or "write",
            arg_preview=redacted_arg,
            source=source,
            expires_at=float(action.expires_at),
        )
        try:
            ext_id = notifier.send(req)
            if ext_id:
                _record_notification(settings, action.token, ch, str(target), ext_id)
        except Exception:
            logger.debug("Failed to send notification on channel %s", ch, exc_info=True)


def _notify_outcome(
    settings: Any,
    token: str,
    status: str,
    via: str | None = None,
    result_preview: str | None = None,
) -> None:
    try:
        conn = _connect(settings)
        try:
            rows = conn.execute(
                "SELECT channel, target, external_id FROM relay_notifications WHERE token = ?",
                (token,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return

        via_label = {
            "web": "Web",
            "telegram": "Telegram",
            "feishu": "飞书",
            "origin": "原渠道",
        }.get(via or "", via or "未知")

        if status in ("approved", "done", "executed", "accepted"):
            text = f"✅ 已批准（{via_label}）"
            if result_preview:
                safe_res = truncate(redact_text(result_preview), 200)
                text += f" · 结果: {safe_res}"
        elif status in ("rejected", "cancelled", "denied"):
            text = f"❌ 已拒绝（{via_label}）"
        elif status == "expired":
            text = "⌛ 已过期"
        elif status == "failed":
            text = f"⚠️ 执行失败（{via_label}）"
            if result_preview:
                safe_res = truncate(redact_text(result_preview), 200)
                text += f" · 错误: {safe_res}"
        else:
            text = f"ℹ️ 状态: {status}（{via_label}）"

        for row in rows:
            ch = row["channel"]
            notifier = get_notifier(ch, settings)
            if notifier:
                try:
                    notifier.update(row["external_id"], row["target"], text)
                except Exception:
                    logger.debug("Failed to update notification %s on channel %s", row["external_id"], ch, exc_info=True)
    except Exception:
        logger.debug("Failed to notify outcome for token %s", token, exc_info=True)


# -----------------------------------------------------------------------------
# Core Relay API
# -----------------------------------------------------------------------------

def publish(
    settings: Any,
    action: PendingToolAction,
    *,
    summary: str = "",
    danger: str = "",
    source: str = "chat",
    origin_instance: str | None = None,
) -> bool:
    """Upsert pending row and send cross-channel notifications."""
    if not is_relay_enabled(settings):
        return False
    try:
        inst = origin_instance or INSTANCE_ID
        conn = _connect(settings)
        try:
            redacted_arg = truncate(redact_text(action.arg or ""), 2000)
            with conn:
                conn.execute(
                    """
                    INSERT INTO relay_approvals (
                        token, origin_channel, origin_pid, origin_instance,
                        tool_name, summary, arg_preview, danger, source,
                        created_at, expires_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT(token) DO UPDATE SET
                        tool_name=excluded.tool_name,
                        summary=excluded.summary,
                        arg_preview=excluded.arg_preview,
                        danger=excluded.danger,
                        source=excluded.source,
                        expires_at=excluded.expires_at
                    WHERE status='pending'
                    """,
                    (
                        action.token,
                        action.channel,
                        PID,
                        inst,
                        action.tool_name,
                        summary or action.tool_name,
                        redacted_arg,
                        danger or "write",
                        source,
                        float(action.created_at),
                        float(action.expires_at),
                    ),
                )
        finally:
            conn.close()

        _dispatch_notification(_notify_publish, settings, action, summary=summary, danger=danger, source=source)
        return True
    except Exception:
        logger.exception("Failed to publish approval %s to relay", action.token)
        return False


def update_arg(settings: Any, token: str, new_arg: str) -> bool:
    """Update arg_preview when draft is edited."""
    if not is_relay_enabled(settings):
        return False
    try:
        redacted = truncate(redact_text(new_arg or ""), 2000)
        conn = _connect(settings)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE relay_approvals SET arg_preview = ? WHERE token = ? AND status = 'pending'",
                    (redacted, token),
                )
                return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to update arg in relay for token %s", token, exc_info=True)
        return False


def decide(
    settings: Any,
    token: str,
    approve: bool,
    *,
    via: str,
    decided_by: str,
) -> str:
    """Atomic compare-and-set decision across all surfaces.

    Returns: 'won' | 'already_decided' | 'expired' | 'unknown'
    """
    if not is_relay_enabled(settings):
        return "unknown"
    now = time.time()
    new_status = "approved" if approve else "rejected"
    clean_decided_by = truncate(redact_text(str(decided_by or "")), 200)

    try:
        conn = _connect(settings)
        try:
            with conn:
                row = conn.execute(
                    "SELECT status, expires_at, tool_name, arg_preview, danger, origin_channel FROM relay_approvals WHERE token = ?",
                    (token,),
                ).fetchone()
                if row is None:
                    return "unknown"
                if row["status"] != "pending":
                    return "already_decided"
                if float(row["expires_at"]) <= now:
                    cur = conn.execute(
                        "UPDATE relay_approvals SET status = 'expired' WHERE token = ? AND status = 'pending'",
                        (token,),
                    )
                    expired_now = cur.rowcount == 1
                    outcome = "expired"
                else:
                    expired_now = False
                    outcome = ""
                if outcome != "expired":
                    cur = conn.execute(
                        """
                        UPDATE relay_approvals
                        SET status = ?, decided_via = ?, decided_by = ?, decided_at = ?
                        WHERE token = ? AND status = 'pending' AND expires_at > ?
                        """,
                        (new_status, via, clean_decided_by, now, token, now),
                    )
                    if cur.rowcount == 1:
                        outcome = "won"
                    else:
                        row2 = conn.execute(
                            "SELECT status, expires_at FROM relay_approvals WHERE token = ?",
                            (token,),
                        ).fetchone()
                        if row2 and float(row2["expires_at"]) <= now:
                            outcome = "expired"
                        else:
                            outcome = "already_decided"
        finally:
            conn.close()

        if outcome == "expired":
            if expired_now:
                _dispatch_notification(_notify_outcome, settings, token, "expired", via="system")
            return outcome
        if outcome == "won":
            audit_tool_event(
                settings,
                operator_id=clean_decided_by,
                chat_id=via,
                channel=via,
                tool_name=row["tool_name"],
                arg=row["arg_preview"],
                danger=row["danger"],
                action="relay_decided",
                via=via,
                decided_by=clean_decided_by,
                outcome=new_status,
            )
            _dispatch_notification(_notify_outcome, settings, token, new_status, via=via)

        return outcome
    except Exception:
        logger.exception("Error in approval_relay.decide for token %s", token)
        return "unknown"


def mark_local(
    settings: Any,
    token: str,
    status: str,
    result_preview: str | None = None,
) -> bool:
    """Mark an action resolved locally by its origin process."""
    if not is_relay_enabled(settings):
        return False
    try:
        now = time.time()
        safe_result = truncate(redact_text(result_preview), 2000) if result_preview is not None else None
        conn = _connect(settings)
        try:
            with conn:
                # Only the first local resolution of a still-open row counts;
                # later marks (e.g. routine bookkeeping after execute_confirmed)
                # are no-ops so other surfaces are updated exactly once.
                cur = conn.execute(
                    """
                    UPDATE relay_approvals
                    SET status = ?,
                        decided_via = COALESCE(decided_via, 'origin'),
                        decided_at = COALESCE(decided_at, ?),
                        result_preview = COALESCE(?, result_preview)
                    WHERE token = ? AND status IN ('pending', 'approved', 'rejected')
                    """,
                    (status, now, safe_result, token),
                )
                changed = cur.rowcount == 1
            row = conn.execute(
                "SELECT decided_via FROM relay_approvals WHERE token = ?",
                (token,),
            ).fetchone()
        finally:
            conn.close()

        if not changed:
            return False
        via = row["decided_via"] if (row and row["decided_via"]) else "origin"
        _dispatch_notification(_notify_outcome, settings, token, status, via=via, result_preview=safe_result)
        return True
    except Exception:
        logger.debug("Failed to mark_local in relay for token %s", token, exc_info=True)
        return False


def claim_local(settings: Any, token: str, approve: bool) -> str:
    """Gate a local (origin-process) resolution against the shared store.

    Called by ``execute_confirmed`` / ``cancel_pending`` before they act, so a
    local button press and a remote decision cannot both win. Returns
    ``"ok"`` when the local action may proceed (relay off, no row, the row was
    still pending and is now decided by the origin, or the row already carries
    the same decision, e.g. the consumer executing a remote approval).
    Otherwise returns the conflicting status (``"rejected"``, ``"approved"``,
    ``"expired"``, ``"done"``, ...).
    """
    if not is_relay_enabled(settings):
        return "ok"
    want = "approved" if approve else "rejected"
    try:
        now = time.time()
        conn = _connect(settings)
        try:
            with conn:
                cur = conn.execute(
                    """
                    UPDATE relay_approvals
                    SET status = ?, decided_via = 'origin', decided_by = 'origin',
                        decided_at = ?, claimed_at = ?
                    WHERE token = ? AND status = 'pending'
                    """,
                    (want, now, now, token),
                )
                if cur.rowcount == 1:
                    won = True
                    current = want
                else:
                    won = False
                    row = conn.execute(
                        "SELECT status FROM relay_approvals WHERE token = ?", (token,)
                    ).fetchone()
                    current = row["status"] if row else None
        finally:
            conn.close()
        if won:
            return "ok"
        if current is None or current == want:
            return "ok"
        return str(current)
    except Exception:
        logger.debug("claim_local failed for token %s; allowing local action", token, exc_info=True)
        return "ok"


def claim_decisions_for_instance(
    settings: Any,
    instance: str,
    local_tokens: list[str],
) -> list[dict[str, Any]]:
    """Atomically claim decided-but-unclaimed rows in local pending store."""
    if not is_relay_enabled(settings) or not local_tokens:
        return []
    try:
        now = time.time()
        conn = _connect(settings)
        claimed: list[dict[str, Any]] = []
        try:
            with conn:
                placeholders = ",".join("?" for _ in local_tokens)
                cur = conn.execute(
                    f"""
                    UPDATE relay_approvals
                    SET claimed_at = ?
                    WHERE token IN ({placeholders})
                      AND status IN ('approved', 'rejected')
                      AND claimed_at IS NULL
                    RETURNING *
                    """,
                    [now] + local_tokens,
                )
                claimed = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        return claimed
    except Exception:
        logger.exception("Failed to claim decisions for instance %s", instance)
        return []


def list_pending(settings: Any) -> list[dict[str, Any]]:
    """Return non-expired pending rows with lazy expiry of stale rows."""
    if not is_relay_enabled(settings):
        return []
    now = time.time()
    try:
        conn = _connect(settings)
        try:
            with conn:
                stale_rows = conn.execute(
                    "SELECT token FROM relay_approvals WHERE status = 'pending' AND expires_at <= ?",
                    (now,),
                ).fetchall()
                if stale_rows:
                    conn.execute(
                        "UPDATE relay_approvals SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
                        (now,),
                    )
                rows = conn.execute(
                    "SELECT * FROM relay_approvals WHERE status = 'pending' ORDER BY created_at DESC"
                ).fetchall()
                result = [dict(r) for r in rows]
        finally:
            conn.close()

        for s in stale_rows:
            _dispatch_notification(_notify_outcome, settings, s["token"], "expired", via="system")

        return result
    except Exception:
        logger.exception("Failed to list pending relay approvals")
        return []


def list_foreign_pending(
    settings: Any,
    current_instance: str | None = None,
    local_tokens: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return pending rows originating from another process instance."""
    if not is_relay_enabled(settings):
        return []
    inst = current_instance or INSTANCE_ID
    all_pending = list_pending(settings)
    local_set = local_tokens or set()
    return [
        row for row in all_pending
        if row["origin_instance"] != inst and row["token"] not in local_set
    ]


def get_relay_row(settings: Any, token: str) -> dict[str, Any] | None:
    """Retrieve row by token."""
    if not is_relay_enabled(settings):
        return None
    try:
        conn = _connect(settings)
        try:
            row = conn.execute("SELECT * FROM relay_approvals WHERE token = ?", (token,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        logger.debug("Failed to get relay row %s", token, exc_info=True)
        return None


# -----------------------------------------------------------------------------
# Outbound Port Wrapper & Relay Consumer
# -----------------------------------------------------------------------------

class PrefixedOutboundPort(OutboundPort):
    supports_inline_buttons = False
    supports_attachments = False

    def __init__(self, inner: OutboundPort, prefix: str) -> None:
        self._inner = inner
        self._prefix = prefix
        self.supports_inline_buttons = getattr(inner, "supports_inline_buttons", False)
        self.supports_attachments = getattr(inner, "supports_attachments", False)

    def _prefix_text(self, text: str) -> str:
        return f"{self._prefix}\n\n{text}" if text else self._prefix

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        return await self._inner.reply(msg, self._prefix_text(text))

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        return await self._inner.send_new(msg, self._prefix_text(text))

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        return await self._inner.edit_progress(msg, placeholder_id, self._prefix_text(text))

    async def reply_with_buttons(self, msg: InboundMessage, text: str, buttons: list[list[dict]]) -> str | None:
        return await self._inner.reply_with_buttons(msg, self._prefix_text(text), buttons)


class RelayConsumer:
    """Background consumer that polls for decisions claimed by this process and executes them."""

    def __init__(
        self,
        settings: Any,
        channel: str,
        *,
        instance_id: str | None = None,
        poll_interval: float = 1.0,
        port_factory: Callable[[str], OutboundPort] | None = None,
        get_local_pending_fn: Callable[[], list[PendingToolAction]] | None = None,
    ) -> None:
        self.settings = settings
        self.channel = channel
        self.instance_id = instance_id or INSTANCE_ID
        self.poll_interval = poll_interval
        self.port_factory = port_factory
        self.get_local_pending_fn = get_local_pending_fn
        self._running = False
        self._task: asyncio.Task | None = None

    def _get_local_actions(self) -> dict[str, PendingToolAction]:
        if self.get_local_pending_fn is not None:
            actions = self.get_local_pending_fn()
        else:
            actions = confirm_list_pending(channel=self.channel)
        return {a.token: a for a in actions}

    async def poll_once(self) -> int:
        if not is_relay_enabled(self.settings):
            return 0
        local_map = self._get_local_actions()
        if not local_map:
            return 0
        local_tokens = list(local_map.keys())
        claimed = claim_decisions_for_instance(self.settings, self.instance_id, local_tokens)
        if not claimed:
            return 0

        for row in claimed:
            token = row["token"]
            action = local_map.get(token) or get_pending(token)
            if action is None:
                mark_local(self.settings, token, "expired")
                continue

            approve = (row["status"] == "approved")
            decided_via = row.get("decided_via") or "unknown"
            via_label = {
                "web": "Web",
                "telegram": "Telegram",
                "feishu": "飞书",
            }.get(decided_via, decided_via)
            prefix = f"✅ 已在 {via_label} 批准" if approve else f"❌ 已在 {via_label} 拒绝"

            if self.channel == "web":
                from web_chat import decide_tool_approval
                res = await decide_tool_approval(action, approve, self.settings)
                # execute_confirmed / cancel_pending already marked the row
                # done/failed/cancelled; only handle the raced-expiry case.
                if res.get("status") == "expired":
                    mark_local(self.settings, token, "expired")
            else:
                from handlers.tools.runner import cancel_pending, execute_confirmed
                msg = InboundMessage(
                    channel=action.channel,
                    operator_id=action.operator_id,
                    chat_id=action.chat_id,
                    message_id=None,
                    text="确认" if approve else "取消",
                    chat_type="p2p",
                )
                if self.port_factory:
                    raw_port = self.port_factory(action.chat_id)
                else:
                    from web_chat import CollectingPort
                    raw_port = CollectingPort()
                prefixed_port = PrefixedOutboundPort(raw_port, prefix)
                if approve:
                    await execute_confirmed(msg, prefixed_port, self.settings, token)
                else:
                    await cancel_pending(msg, prefixed_port, self.settings, token)

        return len(claimed)

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Error in RelayConsumer loop for channel %s", self.channel)
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
        finally:
            self._running = False

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self.run())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


def start_relay_worker(
    loop: asyncio.AbstractEventLoop,
    settings: Any,
    channel: str = "web",
) -> RelayConsumer | None:
    """Start the RelayConsumer on the provided event loop."""
    if not is_relay_enabled(settings):
        return None
    consumer = RelayConsumer(settings, channel=channel)
    loop.create_task(consumer.run())
    return consumer


