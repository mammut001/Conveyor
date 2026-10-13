"""Opt-in, signed and replay-safe event triggers for existing routines."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any
import routines

logger = logging.getLogger("conveyor.event_triggers")
MAX_BYTES = 262144
MAX_PER_HOOK = 30
MAX_GLOBAL = 120
_LOCK = threading.Lock()
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
SIG_PATTERN = re.compile(r"sha256=[0-9a-fA-F]{64}")


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "hooks_enabled", False) and getattr(settings, "routines_enabled", False))


def _db(settings):
    routines.init_db(settings)
    conn = routines._connect(settings)
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS routine_hook_policies (
            hook_id TEXT PRIMARY KEY,
            paused INTEGER NOT NULL DEFAULT 0,
            event_filter_json TEXT NOT NULL DEFAULT '{}',
            mode TEXT NOT NULL DEFAULT 'generic'
        );
        CREATE TABLE IF NOT EXISTS routine_hook_secret_claims (
            hook_id TEXT PRIMARY KEY, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS routine_hook_receipts (
            hook_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
            accepted_at REAL NOT NULL, PRIMARY KEY (hook_id, delivery_id)
        );
        CREATE INDEX IF NOT EXISTS idx_hook_receipts_time ON routine_hook_receipts(accepted_at);
        """)
        # Existing installations already have policies without this column.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(routine_hook_policies)")}
        if "mode" not in columns:
            conn.execute("ALTER TABLE routine_hook_policies ADD COLUMN mode TEXT NOT NULL DEFAULT 'generic'")
    return conn


def validate_mode(mode):
    if mode not in ("generic", "github"):
        raise ValueError("mode must be generic or github")
    return mode


def validate_filter(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"event", "actions", "branch"}:
        raise ValueError("filter must contain only event, actions, branch")
    result = {}
    if "event" in value:
        event = value["event"]
        if not isinstance(event, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", event):
            raise ValueError("invalid event filter")
        result["event"] = event
    if "actions" in value:
        actions = value["actions"]
        if not isinstance(actions, list) or not 1 <= len(actions) <= 20 or any(
            not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", a) for a in actions
        ):
            raise ValueError("actions must be 1-20 valid action names")
        result["actions"] = list(dict.fromkeys(actions))
    if "branch" in value:
        branch = value["branch"]
        if not isinstance(branch, str) or not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]{1,180}", branch) or ".." in branch:
            raise ValueError("invalid branch ref")
        result["branch"] = branch
    if ("branch" in result or "actions" in result) and "event" not in result:
        raise ValueError("event required for actions/branch filter")
    if "branch" in result and result["event"] != "push":
        raise ValueError("branch filtering only supports push")
    return result


def _policy(conn, hook_id):
    row = conn.execute("SELECT paused,event_filter_json,mode FROM routine_hook_policies WHERE hook_id=?", (hook_id,)).fetchone()
    return {"paused": bool(row["paused"]), "filter": json.loads(row["event_filter_json"]),
            "mode": row["mode"]} if row else {"paused": False, "filter": {}, "mode": "generic"}


def get_hook(settings, hook_id):
    conn = _db(settings)
    try:
        row = conn.execute(
            "SELECT h.*, r.enabled AS routine_enabled FROM routine_hooks h "
            "JOIN routines r ON r.id=h.routine_id WHERE h.hook_id=?", (hook_id,),
        ).fetchone()
        return {**dict(row), **_policy(conn, hook_id)} if row else None
    finally:
        conn.close()


def list_hooks(settings):
    conn = _db(settings)
    try:
        rows = conn.execute(
            "SELECT h.hook_id,h.routine_id,h.created_at,h.last_fired_at,h.fire_count,"
            "r.name,r.enabled AS routine_enabled FROM routine_hooks h "
            "JOIN routines r ON r.id=h.routine_id ORDER BY h.routine_id"
        ).fetchall()
        return [{**dict(row), **_policy(conn, row["hook_id"]),
                 "routine_enabled": bool(row["routine_enabled"]),
                 "path": "/hooks/" + row["hook_id"]} for row in rows]
    finally:
        conn.close()


def create_hook(settings, routine_id, event_filter=None, mode="generic", *, claim_in_web=False):
    filt = validate_filter(event_filter)
    mode = validate_mode(mode)
    # Existing routines store generates random secret and chmods routines.db 0600.
    hook = routines.create_or_rotate_hook(settings, routine_id)
    conn = _db(settings)
    try:
        with conn:
            conn.execute("DELETE FROM routine_hook_policies WHERE hook_id NOT IN (SELECT hook_id FROM routine_hooks)")
            conn.execute("DELETE FROM routine_hook_secret_claims WHERE hook_id NOT IN (SELECT hook_id FROM routine_hooks)")
            conn.execute("INSERT OR REPLACE INTO routine_hook_policies(hook_id,paused,event_filter_json,mode) VALUES (?,0,?,?)",
                         (hook["hook_id"], json.dumps(filt), mode))
            if claim_in_web:
                conn.execute("INSERT INTO routine_hook_secret_claims(hook_id,created_at) VALUES (?,?)",
                             (hook["hook_id"], time.time()))
        return {**hook, "paused": False, "filter": filt, "mode": mode}
    finally:
        conn.close()


def claim_secret_once(settings, hook_id):
    """Consume a web-only secret claim atomically; never return it to chat."""
    conn = _db(settings)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT h.secret FROM routine_hook_secret_claims c "
                "JOIN routine_hooks h ON h.hook_id=c.hook_id WHERE c.hook_id=?", (hook_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM routine_hook_secret_claims WHERE hook_id=?", (hook_id,))
            return row["secret"]
    finally:
        conn.close()


def rotate_hook(settings, hook_id, *, claim_in_web=False):
    old = get_hook(settings, hook_id)
    if not old:
        raise KeyError("hook not found")
    fresh = create_hook(settings, old["routine_id"], old["filter"], old["mode"], claim_in_web=claim_in_web)
    if old["paused"]:
        set_paused(settings, fresh["hook_id"], True)
        fresh["paused"] = True
    return fresh


def set_paused(settings, hook_id, paused):
    conn = _db(settings)
    try:
        with conn:
            if not conn.execute("SELECT 1 FROM routine_hooks WHERE hook_id=?", (hook_id,)).fetchone():
                raise KeyError("hook not found")
            conn.execute("INSERT INTO routine_hook_policies(hook_id,paused) VALUES (?,?) "
                         "ON CONFLICT(hook_id) DO UPDATE SET paused=excluded.paused",
                         (hook_id, int(paused)))
        return {"hook_id": hook_id, "paused": bool(paused)}
    finally:
        conn.close()


def delete_hook(settings, hook_id):
    conn = _db(settings)
    try:
        with conn:
            result = conn.execute("DELETE FROM routine_hooks WHERE hook_id=?", (hook_id,))
            conn.execute("DELETE FROM routine_hook_policies WHERE hook_id=?", (hook_id,))
            conn.execute("DELETE FROM routine_hook_secret_claims WHERE hook_id=?", (hook_id,))
        return result.rowcount > 0
    finally:
        conn.close()


def event_matches(filt, event, payload):
    return (not filt.get("event") or filt["event"] == event) and (
        not filt.get("actions") or payload.get("action") in filt["actions"]
    ) and (not filt.get("branch") or payload.get("ref") == filt["branch"])


def reserve_delivery(settings, hook_id, delivery_id, now=None):
    """Atomically claim nonce and enforce per-hook/global rolling-minute limits."""
    t = time.time() if now is None else now
    conn = _db(settings)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM routine_hook_receipts WHERE accepted_at < ?", (t - 7 * 86400,))
            if conn.execute("SELECT 1 FROM routine_hook_receipts WHERE hook_id=? AND delivery_id=?",
                            (hook_id, delivery_id)).fetchone():
                return "replay"
            if conn.execute("SELECT count(*) FROM routine_hook_receipts WHERE hook_id=? AND accepted_at>?",
                            (hook_id, t - 60)).fetchone()[0] >= MAX_PER_HOOK:
                return "rate"
            if conn.execute("SELECT count(*) FROM routine_hook_receipts WHERE accepted_at>?",
                            (t - 60,)).fetchone()[0] >= MAX_GLOBAL:
                return "rate"
            conn.execute("INSERT INTO routine_hook_receipts VALUES (?,?,?)", (hook_id, delivery_id, t))
            return "ok"
    finally:
        conn.close()

    
def delivery_status(settings, hook_id, delivery_id, now=None):
    """Read-only admission check, before routine-busy (does not burn nonce)."""
    t = time.time() if now is None else now
    conn = _db(settings)
    try:
        if conn.execute("SELECT 1 FROM routine_hook_receipts WHERE hook_id=? AND delivery_id=?",
                        (hook_id, delivery_id)).fetchone():
            return "replay"
        if conn.execute("SELECT count(*) FROM routine_hook_receipts WHERE hook_id=? AND accepted_at>?",
                        (hook_id, t - 60)).fetchone()[0] >= MAX_PER_HOOK:
            return "rate"
        if conn.execute("SELECT count(*) FROM routine_hook_receipts WHERE accepted_at>?",
                        (t - 60,)).fetchone()[0] >= MAX_GLOBAL:
            return "rate"
        return "ok"
    finally:
        conn.close()


def _settings(h):
    return getattr(h.server.control, "settings", None)


def _error(h, hook_id, status, reason, event="webhook"):
    safe_event = re.sub(r"[^A-Za-z0-9_.-]", "", str(event))[:64]
    logger.info("hook [%s] status=%d event=%s", hook_id[:8], status, safe_event)
    h._json(status, {"error": reason})


def receive(h, hook_id):
    settings = _settings(h)
    if not enabled(settings):
        h._drain_body()
        _error(h, hook_id, 404, "not found")
        return
    hook = get_hook(settings, hook_id)
    if not hook:
        h._drain_body()
        _error(h, hook_id, 404, "not found")
        return

    # Exact Content-Length framing. Every bounded body is fully read before 400/401.
    if h.headers.get("Transfer-Encoding") is not None:
        h.close_connection = True
        _error(h, hook_id, 400, "invalid framing")
        return
    raw_len = h.headers.get("Content-Length")
    if raw_len is None:
        h.close_connection = True
        _error(h, hook_id, 411, "content-length required")
        return
    try:
        size = int(raw_len)
    except ValueError:
        h.close_connection = True
        _error(h, hook_id, 400, "invalid length")
        return
    if size < 0 or size > MAX_BYTES:
        h.close_connection = True
        _error(h, hook_id, 413, "payload too large")
        return
    body = h.rfile.read(size)
    if len(body) != size:
        h.close_connection = True
        _error(h, hook_id, 400, "incomplete body")
        return

    # Mode is stored at creation, never inferred from attacker-supplied headers.
    github = hook["mode"] == "github"
    github_headers = ("X-Hub-Signature-256", "X-GitHub-Delivery", "X-GitHub-Event")
    conveyor_headers = ("X-Conveyor-Signature", "X-Conveyor-Timestamp",
                        "X-Conveyor-Delivery", "X-Conveyor-Event")
    forbidden = conveyor_headers if github else github_headers
    if any(h.headers.get(name) is not None for name in forbidden):
        _error(h, hook_id, 401, "wrong hook mode")
        return
    delivery_id = h.headers.get("X-GitHub-Delivery" if github else "X-Conveyor-Delivery", "")
    if not ID_PATTERN.fullmatch(delivery_id):
        _error(h, hook_id, 401, "delivery id required")
        return
    if github:
        # Native GitHub sends no timestamp; HMAC authenticates the exact raw body.
        signature = h.headers.get("X-Hub-Signature-256", "")
        if not h.headers.get("X-GitHub-Event"):
            _error(h, hook_id, 401, "GitHub event required")
            return
        signed_body = body
    else:
        stamp = h.headers.get("X-Conveyor-Timestamp", "")
        if not re.fullmatch(r"[0-9]{1,16}", stamp):
            _error(h, hook_id, 401, "invalid timestamp")
            return
        if abs(time.time() - int(stamp)) > 300:
            _error(h, hook_id, 401, "stale timestamp")
            return
        signature = h.headers.get("X-Conveyor-Signature", "")
        signed_body = f"{stamp}.{delivery_id}.".encode("ascii") + body
    if not SIG_PATTERN.fullmatch(signature) or not routines.verify_signature(hook["secret"], signed_body, signature):
        _error(h, hook_id, 401, "invalid signature")
        return
    if h.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        _error(h, hook_id, 415, "application/json required")
        return
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        _error(h, hook_id, 400, "invalid JSON")
        return
    if not isinstance(payload, dict):
        _error(h, hook_id, 400, "JSON object required")
        return
    event = h.headers.get("X-GitHub-Event" if github else "X-Conveyor-Event", "webhook")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", event):
        _error(h, hook_id, 400, "invalid event")
        return
    if hook["paused"] or not hook["routine_enabled"]:
        _error(h, hook_id, 409, "hook or routine paused", event)
        return
    if not event_matches(hook["filter"], event, payload):
        h._json(202, {"ok": True, "filtered": True})
        return
    routine = routines.get_routine(settings, hook["routine_id"])
    if not routine or not routine["enabled"]:
        _error(h, hook_id, 409, "routine paused", event)
        return
    lock = getattr(h.server, "_rate_limit_lock", _LOCK)
    rid = int(routine["id"])
    with lock:
        active = getattr(h.server, "_active_routine_runs", None)
        if active is None:
            active = set()
            h.server._active_routine_runs = active
        # Check replay and quotas before busy, but reserve the nonce only if accepted.
        result = delivery_status(settings, hook_id, delivery_id)
        if result != "ok":
            status = 409 if result == "replay" else 429
            _error(h, hook_id, status, "replayed delivery" if result == "replay" else "rate limit", event)
            return
        if rid in active:
            h._json(429, {"error": "routine busy"}, extra_headers=[("Retry-After", "5")])
            return
        result = reserve_delivery(settings, hook_id, delivery_id)
        if result != "ok":
            status = 409 if result == "replay" else 429
            _error(h, hook_id, status, "replayed delivery" if result == "replay" else "rate limit", event)
            return
        active.add(rid)

    event_data = {"type": event, "payload": routines.format_webhook_payload(body)}
    async def execute():
        try:
            await routines.run_single_routine(settings, getattr(h.server.control, "runner", None),
                                              routine, trigger="webhook", event=event_data)
        except Exception:
            logger.exception("hook routine execution failed for #%d", rid)
        finally:
            with lock:
                active.discard(rid)
    try:
        asyncio.run_coroutine_threadsafe(execute(), h.server.loop)
    except Exception:
        with lock:
            active.discard(rid)
        logger.exception("failed to enqueue hook run")
        _error(h, hook_id, 503, "unavailable", event)
        return
    routines.record_hook_fired(settings, hook_id)
    logger.info("hook [%s] status=202 event=%s", hook_id[:8], event)
    h._json(202, {"ok": True, "accepted": True})


def api_get(h, path):
    parts = path.strip("/").split("/")
    if path != "/api/hooks" and not (len(parts) == 4 and parts[:2] == ["api", "hooks"] and parts[3] == "secret"):
        return False
    if not enabled(_settings(h)):
        h._json(409, {"error": "hooks disabled"})
    elif path == "/api/hooks":
        h._json(200, {"hooks": list_hooks(_settings(h))})
    else:
        secret = claim_secret_once(_settings(h), parts[2])
        h._json(200, {"secret": secret}) if secret is not None else h._json(404, {"error": "secret unavailable"})
    return True


def api_post(h, path):
    if path != "/api/hooks" and not path.startswith("/api/hooks/"):
        return False
    settings = _settings(h)
    if not enabled(settings):
        h._drain_body()
        h._json(409, {"error": "hooks disabled"})
        return True
    body = h._body()
    if body is None:
        return True
    try:
        if path == "/api/hooks":
            if isinstance(body.get("routine_id"), bool):
                raise ValueError("invalid routine id")
            item = create_hook(settings, int(body.get("routine_id", 0)),
                               body.get("filter"), body.get("mode", "generic"))
            h._json(201, item)
        else:
            parts = path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "hooks"] or parts[3] not in ("rotate", "pause", "resume"):
                h._json(404, {"error": "not found"})
            elif parts[3] == "rotate":
                h._json(201, rotate_hook(settings, parts[2]))
            else:
                h._json(200, set_paused(settings, parts[2], parts[3] == "pause"))
    except KeyError:
        h._json(404, {"error": "not found"})
    except (ValueError, TypeError) as exc:
        h._json(400, {"error": str(exc)})
    return True


def api_delete(h, path):
    if not path.startswith("/api/hooks/"):
        return False
    if not enabled(_settings(h)):
        h._json(409, {"error": "hooks disabled"})
        return True
    parts = path.strip("/").split("/")
    if len(parts) != 3:
        h._json(404, {"error": "not found"})
    else:
        ok = delete_hook(_settings(h), parts[2])
        h._json(200 if ok else 404, {"ok": ok} if ok else {"error": "not found"})
    return True
