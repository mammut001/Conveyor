"""Opt-in Web Push for the single-operator private Web Console.

Only opaque IDs and a generic title leave the private network. No payload
content, action, prompt, draft, VAPID secret, or approval capability is sent.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import ipaddress
import json
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_TRUE = {"1", "true", "yes", "on"}
_QUEUE: "queue.Queue[tuple[object, str, str]]" = queue.Queue(maxsize=128)
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


def enabled(settings: object) -> bool:
    return bool(getattr(settings, "pwa_enabled", False))


def operator(settings: object) -> str:
    # A console bearer token authenticates this one configured operator.
    # Never trust an operator id sent by a subscribing browser.
    return str(getattr(settings, "operator_name", None) or "web-console")


def public_config(settings: object) -> dict:
    active = enabled(settings) and bool(os.getenv("CONVEYOR_VAPID_PUBLIC_KEY") and os.getenv("CONVEYOR_VAPID_PRIVATE_KEY"))
    return {"enabled": active, "public_key": os.getenv("CONVEYOR_VAPID_PUBLIC_KEY", "") if active else ""}


def _db_path(settings: object) -> Path:
    return Path(getattr(settings, "codex_memory_root")) / "state" / "web_push.sqlite3"


def _connect(settings: object) -> sqlite3.Connection:
    path = _db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
        id TEXT PRIMARY KEY, operator TEXT NOT NULL, endpoint TEXT NOT NULL,
        p256dh TEXT NOT NULL, auth TEXT NOT NULL, created REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_push_operator ON push_subscriptions(operator)")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _connection(settings: object):
    conn = _connect(settings)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _valid_subscription(body: object) -> tuple[str, str, str]:
    if not isinstance(body, dict):
        raise ValueError("subscription object required")
    sub = body.get("subscription", body)
    if not isinstance(sub, dict):
        raise ValueError("subscription object required")
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if not isinstance(endpoint, str) or not isinstance(keys, dict) or len(endpoint) > 2048:
        raise ValueError("invalid subscription")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443) or any(ch.isspace() for ch in endpoint)):
        raise ValueError("invalid push endpoint")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("invalid push endpoint")
    try:
        ipaddress.ip_address(host)
        raise ValueError("IP push endpoints are not allowed")
    except ValueError as exc:
        if str(exc) == "IP push endpoints are not allowed":
            raise
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(x, str) and 12 <= len(x) <= 256 and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", x) for x in (p256dh, auth)):
        raise ValueError("invalid subscription keys")
    return endpoint, p256dh, auth


def subscribe(settings: object, body: object) -> str:
    if not enabled(settings):
        raise ValueError("pwa disabled")
    endpoint, p256dh, auth = _valid_subscription(body)
    sub_id = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    with _connection(settings) as db:
        db.execute(
            """INSERT INTO push_subscriptions (id,operator,endpoint,p256dh,auth,created)
               VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               operator=excluded.operator, endpoint=excluded.endpoint,
               p256dh=excluded.p256dh, auth=excluded.auth, created=excluded.created""",
            (sub_id, operator(settings), endpoint, p256dh, auth, time.time()),
        )
        # Bound retained endpoints on each operator; avoid unbounded data growth.
        db.execute(
            """DELETE FROM push_subscriptions WHERE operator=? AND id NOT IN
               (SELECT id FROM push_subscriptions WHERE operator=? ORDER BY created DESC LIMIT 10)""",
            (operator(settings), operator(settings)),
        )
    return sub_id


def unsubscribe(settings: object, sub_id: object) -> None:
    if not enabled(settings):
        return
    if not isinstance(sub_id, str) or not re.fullmatch(r"[0-9a-f]{64}", sub_id):
        raise ValueError("invalid subscription id")
    with _connection(settings) as db:
        db.execute("DELETE FROM push_subscriptions WHERE id=? AND operator=?", (sub_id, operator(settings)))


def _default_sender(subscription: dict, payload: str) -> None:
    # Optional dependency is only imported when feature is actually used.
    from pywebpush import webpush
    webpush(
        subscription_info=subscription, data=payload,
        vapid_private_key=os.environ["CONVEYOR_VAPID_PRIVATE_KEY"],
        vapid_claims={"sub": "mailto:push@conveyor.invalid"},
        ttl=120,
        timeout=5,
    )


def notify_pending(settings: object, item_id: str, kind: str = "approval", *, sender=None) -> int:
    """Send safe minimal payloads. Injectable sender enables offline tests."""
    if not enabled(settings) or (sender is None and not public_config(settings)["enabled"]):
        return 0
    if kind not in ("approval", "draft") or not isinstance(item_id, str) or not 1 <= len(item_id) <= 128:
        return 0
    if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
        return 0
    path = _db_path(settings)
    if not path.exists():
        return 0
    with _connection(settings) as db:
        rows = db.execute("SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE operator=?",
                          (operator(settings),)).fetchall()
    data = json.dumps({"title": "Conveyor needs your review", "id": item_id}, separators=(",", ":"))
    sent = 0
    for row in rows:
        sub = {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
        try:
            (sender or _default_sender)(sub, data)
            sent += 1
        except Exception as exc:
            # No exception text or traceback: transport/library errors may embed
            # endpoints or key material.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                with _connection(settings) as db:
                    db.execute("DELETE FROM push_subscriptions WHERE id=? AND operator=?",
                               (row["id"], operator(settings)))
    return sent


def _worker() -> None:
    while True:
        settings, item_id, kind = _QUEUE.get()
        try:
            notify_pending(settings, item_id, kind)
        except Exception:
            # Push transport must never interrupt approval processing or print secrets.
            pass
        finally:
            _QUEUE.task_done()


def queue_notification(settings: object, item_id: str, kind: str = "approval") -> None:
    """Non-blocking, bounded notification from any approval producer."""
    global _WORKER_STARTED
    if not enabled(settings) or not public_config(settings)["enabled"]:
        return
    if kind not in ("approval", "draft") or not isinstance(item_id, str):
        return
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            threading.Thread(target=_worker, name="conveyor-web-push", daemon=True).start()
            _WORKER_STARTED = True
    try:
        _QUEUE.put_nowait((settings, item_id, kind))
    except queue.Full:
        pass


def api_get(handler: object) -> None:
    settings = handler.server.control.settings
    if not enabled(settings):
        handler._json(404, {"error": "not found"})
        return
    handler._json(200, public_config(settings))


def api_post(handler: object, body: object) -> None:
    settings = handler.server.control.settings
    if not enabled(settings):
        handler._json(404, {"error": "not found"})
        return
    if not public_config(settings)["enabled"]:
        handler._json(503, {"error": "push not configured"})
        return
    try:
        sub_id = subscribe(settings, body)
    except ValueError:
        handler._json(400, {"error": "invalid subscription"})
        return
    handler._json(201, {"id": sub_id})


def api_delete(handler: object) -> None:
    settings = handler.server.control.settings
    if not enabled(settings):
        handler._drain_body()
        handler._json(404, {"error": "not found"})
        return
    body = handler._body()
    if body is None:
        return
    try:
        unsubscribe(settings, body.get("id"))
    except ValueError:
        handler._json(400, {"error": "invalid subscription id"})
        return
    handler._json(200, {"ok": True})
