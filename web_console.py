#!/usr/bin/env python3
"""Authenticated lightweight HTTP/SSE server for the Conveyor Web Console."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from channel.types import InboundMessage
from config import load_runtime_settings
from handlers.job_queue import get_job_queue
from handlers.jobs import submit_codex_job
from logging_setup import configure_logging
from redaction import redact_text
from runner import CodexRunner, JobMode
from web_control import WebControl
from transcript_store import session_identity

logger = logging.getLogger("conveyor.web")
MAX_BODY_BYTES = 65_536
STATIC_ROOT = Path(__file__).resolve().parent / "web" / "dist"


class WebOutbound:
    """Outbound port for Web Console requests.

    Routes replies to TranscriptStore (for browser chat UI persistence)
    and emits real-time events via emit_event.
    """

    supports_inline_buttons = False
    supports_attachments = False
    wait_for_job = False

    def __init__(
        self,
        settings: Any = None,
        durable_session_id: str = "",
        *,
        is_codex: bool = False,
        prompt: str = "",
    ) -> None:
        self.settings = settings
        self.durable_session_id = durable_session_id
        self.is_codex = is_codex
        self.prompt = prompt
        self.last_job: Any = None
        self.turn_id = uuid.uuid4().hex
        self._delivered_final = False
        self.handles_transcript_directly = True

    def on_job_submitted(self, job: Any) -> None:
        self.last_job = job

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        if text in ("💭 …", "⏳ 收到，处理中...", "⏳ 收到, 处理中..."):
            return "web-placeholder"
        if self.is_codex:
            return "web-reply"
        self._persist_turn(msg, text, kind="chat")
        return "web-reply"

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        if self.is_codex:
            return "web-event"
        self._persist_turn(msg, text, kind="chat")
        return "web-event"

    async def edit_progress(self, msg: InboundMessage, _placeholder_id: str, text: str) -> bool:
        if self.is_codex:
            return True
        # Streaming chunk: ends with " ▍" or is intermediate search status
        if text.endswith(" ▍") or text.startswith("🔎 搜索：") or text.startswith("↪️"):
            clean = text[:-2].strip() if text.endswith(" ▍") else text
            if self.settings:
                try:
                    from agent_events import emit_event
                    emit_event(
                        self.settings,
                        "assistant.delta",
                        self.turn_id,
                        {"text": clean},
                        session_id=msg.chat_id,
                    )
                except Exception:
                    logger.debug("Could not emit assistant.delta", exc_info=True)
            return True
        # Final answer delivered via edit_progress
        self._persist_turn(msg, text, kind="chat")
        return True

    async def reply_with_buttons(self, msg: InboundMessage, text: str, _buttons: list[list[dict]]) -> str | None:
        return await self.reply(msg, text)

    async def send_image(self, _chat_id: str, _image_path: str, *, caption: str | None = None) -> None:
        return None

    def _persist_turn(self, msg: InboundMessage, text: str, *, kind: str = "chat") -> None:
        if self._delivered_final or not self.settings or not self.durable_session_id:
            return
        self._delivered_final = True
        user_text = self.prompt or msg.text
        try:
            from transcript_store import get_transcript_store
            get_transcript_store(self.settings).append_turn(
                self.durable_session_id,
                user_text,
                text,
                channel=msg.channel,
                operator_id=msg.operator_id,
                source_chat_id=msg.chat_id,
                kind=kind,
            )
        except Exception:
            logger.exception("Failed to persist web turn to TranscriptStore")
        try:
            from agent_events import emit_event
            emit_event(
                self.settings,
                "assistant.completed",
                self.turn_id,
                {"text": text},
                session_id=msg.chat_id,
            )
        except Exception:
            logger.debug("Failed to emit assistant.completed", exc_info=True)


class WebConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *,
                 control: WebControl, loop: asyncio.AbstractEventLoop, token: str) -> None:
        super().__init__(address, handler)
        self.control = control
        self.loop = loop
        self.token = token
        self._active_routine_runs: set[int] = set()
        self._hook_last_accepted: dict[str, float] = {}
        self._rate_limit_lock = threading.Lock()

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            logger.debug("Web client disconnected: %s", client_address[0])
            return
        super().handle_error(request, client_address)


class WebConsoleHandler(BaseHTTPRequestHandler):
    server_version = "Conveyor"
    sys_version = ""
    server: WebConsoleServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.client_address[0], fmt % args)

    def _headers(self, status: int, content_type: str, length: int | None = None,
                 extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("application/json"):
            cache_control = "no-store"
        elif content_type.startswith("text/html"):
            # The HTML points at hashed assets. Revalidate it on every reload
            # so a production rollout cannot leave the operator on an old UI.
            cache_control = "no-cache"
        else:
            cache_control = "public, max-age=31536000, immutable"
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _json(self, status: int, value: Any, extra_headers: list[tuple[str, str]] | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data), extra_headers=extra_headers)
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _drain_body(self) -> None:
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            return
        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            return
        try:
            length = int(cl_header.strip())
        except ValueError:
            self.close_connection = True
            return
        if length == 0:
            return
        if 1 <= length <= MAX_BODY_BYTES:
            try:
                data = self.rfile.read(length)
                if len(data) < length:
                    self.close_connection = True
            except Exception:
                self.close_connection = True
        else:
            self.close_connection = True

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._drain_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _body(self) -> dict[str, Any] | None:
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        cl_header = self.headers.get("Content-Length")
        if cl_header is None or not cl_header.strip():
            # Bodiless POST (e.g. fetch without body, curl -X POST): treat as {}.
            return {}
        try:
            length = int(cl_header.strip())
        except ValueError:
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        if length == 0:
            return {}
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body size"})
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return None
        if not isinstance(value, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "json object required"})
            return None
        return value

    def _await(self, coro: Any, timeout: float = 30.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.server.loop)
        return future.result(timeout=timeout)

    @staticmethod
    def _segments(path: str) -> list[str]:
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def _memory_settings(self) -> Any:
        """Settings when long-term memory is on; otherwise answer 409 and return None."""
        settings = getattr(self.server.control, "settings", None)
        if settings is None:
            from config import load_runtime_settings
            settings = load_runtime_settings()
        if not getattr(settings, "long_term_memory_enabled", False):
            self._json(HTTPStatus.CONFLICT, {"error": "long-term memory is disabled (set CONVEYOR_LONG_TERM_MEMORY=true)"})
            return None
        return settings

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "conveyor-web", "schema_version": 1})
            return
        if path.startswith("/api/") and not self._require_auth():
            return
        query = parse_qs(parsed.query)
        parts = self._segments(path)
        try:
            if path == "/api/system/status":
                self._json(HTTPStatus.OK, self.server.control.system_status())
            elif path == "/api/config/provider":
                self._json(HTTPStatus.OK, self.server.control.provider_config())
            elif path == "/api/sessions":
                self._json(HTTPStatus.OK, {"sessions": self.server.control.list_sessions()})
            elif len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                item = self.server.control.get_session(parts[2])
                self._json(HTTPStatus.OK if item else HTTPStatus.NOT_FOUND, item or {"error": "not found"})
            elif path == "/api/jobs":
                limit = int((query.get("limit") or ["100"])[0])
                self._json(HTTPStatus.OK, {"jobs": self.server.control.list_jobs(limit)})
            elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                item = self.server.control.get_job(parts[2])
                self._json(HTTPStatus.OK if item else HTTPStatus.NOT_FOUND, item or {"error": "not found"})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "events":
                after = int((query.get("after") or ["0"])[0])
                self._json(HTTPStatus.OK, {"events": self.server.control.events(parts[2], after)})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "diff":
                item = self._await(self.server.control.diff(parts[2]))
                self._json(HTTPStatus.OK if item else HTTPStatus.NOT_FOUND, item or {"error": "not found"})
            elif path == "/api/approvals":
                self._json(HTTPStatus.OK, {"approvals": self.server.control.list_approvals()})
            elif path == "/api/approval-inbox":
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "approval_inbox_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "approval inbox is disabled (set CONVEYOR_APPROVAL_INBOX_ENABLED=true)"})
                    return
                import approval_inbox
                items = approval_inbox.list_items(settings, self.server.control)
                counts = {
                    "total": len(items),
                    "chat": sum(1 for it in items if it.get("source") == "chat"),
                    "routine": sum(1 for it in items if it.get("source") == "routine"),
                    "webhook": sum(1 for it in items if it.get("source") == "webhook"),
                    "job": sum(1 for it in items if it.get("source") == "job"),
                }
                self._json(HTTPStatus.OK, {"items": items, "counts": counts})
            elif path == "/api/memory":
                settings = self._memory_settings()
                if settings is None:
                    return
                from personal_tools import long_term_memory as ltm
                kind = (query.get("kind") or [""])[0].strip() or None
                if kind not in (None, "profile", "log"):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "kind must be profile or log"})
                    return
                q = (query.get("q") or [""])[0].strip()
                try:
                    limit = max(1, min(500, int((query.get("limit") or ["200"])[0])))
                except ValueError:
                    limit = 200
                self._json(HTTPStatus.OK, ltm.api_list(settings, q=q, kind=kind, limit=limit))
            elif path == "/api/routines" or path.startswith("/api/routines"):
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "routines_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)"})
                    return
                import routines
                self._json(HTTPStatus.OK, {"routines": routines.list_routines(settings)})
            elif path == "/api/inbox" or path.startswith("/api/inbox"):
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "routines_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)"})
                    return
                limit = int((query.get("limit") or ["50"])[0])
                import routines
                items, unread = routines.list_inbox(settings, limit=limit)
                self._json(HTTPStatus.OK, {"items": items, "unread": unread})
            elif path == "/api/takeover/status":
                # Plain web console has no takeover routes (see web_console_takeover.py);
                # answer explicitly so the panel stops polling instead of hitting 404s.
                self._json(HTTPStatus.OK, {"available": False, "enabled": False})
            elif path == "/api/chat/history":
                session_id = str((query.get("session_id") or [""])[0]).strip()
                if not session_id:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "session_id is required"})
                    return
                from web_chat import build_history
                # Unknown (e.g. brand-new, not yet persisted) sessions are simply empty.
                session = self.server.control.get_session(session_id)
                self._json(HTTPStatus.OK, {
                    "session_id": session_id,
                    "messages": build_history(session),
                })
            elif path == "/api/nodes":
                self._json(HTTPStatus.OK, {"nodes": self.server.control.nodes()})
            elif path == "/api/computer/status":
                self._json(HTTPStatus.OK, self.server.control.computer_status())
            elif len(parts) == 3 and parts[:2] == ["api", "artifacts"]:
                self._artifact(parts[2])
            elif len(parts) == 3 and parts[:2] == ["api", "nodes"]:
                item = next((node for node in self.server.control.nodes() if node["id"] == parts[2]), None)
                self._json(HTTPStatus.OK if item else HTTPStatus.NOT_FOUND, item or {"error": "not found"})
            elif path == "/api/events/stream":
                self._stream_events(query)
            elif path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self._static(path)
        except (ValueError, TimeoutError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": redact_text(str(exc))})
        except Exception:
            logger.exception("GET request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    async def _handle_task(
        self,
        msg: InboundMessage,
        outbound: WebOutbound,
        settings: Any,
        runner: Any,
        mode: JobMode,
        prompt: str,
    ) -> tuple[bool, str, Any]:
        if mode == JobMode.FIX:
            return await submit_codex_job(
                msg, outbound, runner, mode=mode, prompt=prompt, wait=False,
            )

        from handlers.commands import parse_command
        from handlers.memo import detect_memory_intent
        from handlers.chat import chat_enabled, needs_agent

        parsed = parse_command(msg.text)
        is_cmd = parsed is not None
        cmd_name = parsed[0] if parsed else ""
        if cmd_name in ("run", "fix"):
            job_mode = JobMode.FIX if cmd_name == "fix" else JobMode.RUN
            job_prompt = parsed[1] if parsed else prompt
            return await submit_codex_job(
                msg, outbound, runner, mode=job_mode, prompt=job_prompt, wait=False,
            )

        can_chat = chat_enabled(settings) and not needs_agent(msg.text)
        if can_chat or is_cmd or detect_memory_intent(msg.text):
            from handlers.dispatch import dispatch
            await dispatch(msg, outbound, settings, runner)
            job = outbound.last_job
            return True, "task queued" if job else "ok", job

        # Default fallback to Codex job when execution is needed or chat tier is off
        return await submit_codex_job(
            msg, outbound, runner, mode=JobMode.RUN, prompt=prompt, wait=False,
        )

    def _audit_webhook(self, hook_id: str, status_code: int, event_type: str) -> None:
        prefix = hook_id[:8] if len(hook_id) >= 8 else hook_id
        logger.info("webhook delivery [%s] status=%d event=%s", prefix, status_code, event_type)

    def _handle_webhook(self, hook_id: str) -> None:
        raw_event = self.headers.get("X-GitHub-Event") or self.headers.get("X-Conveyor-Event") or "webhook"
        event_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(raw_event))[:64] or "webhook"

        settings = getattr(self.server.control, "settings", None)
        if settings is None:
            settings = load_runtime_settings()

        import routines

        # 1. Flag off or unknown hook_id -> 404 {"error": "not found"} (drain body).
        webhooks_on = bool(getattr(settings, "webhooks_enabled", False))
        routines_on = bool(getattr(settings, "routines_enabled", False))
        if not (webhooks_on and routines_on):
            self._drain_body()
            self._audit_webhook(hook_id, HTTPStatus.NOT_FOUND, event_type)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        hook = routines.get_hook_by_id(settings, hook_id)
        if hook is None:
            self._drain_body()
            self._audit_webhook(hook_id, HTTPStatus.NOT_FOUND, event_type)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        # 2. Content-Length required, <= 65536 (existing MAX_BODY_BYTES) else 413; read raw bytes exactly once.
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            self._audit_webhook(hook_id, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, event_type)
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid content length"})
            return

        cl_header = self.headers.get("Content-Length")
        if cl_header is None or not cl_header.strip():
            self._drain_body()
            self._audit_webhook(hook_id, HTTPStatus.LENGTH_REQUIRED, event_type)
            self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "content-length required"})
            return

        try:
            length = int(cl_header.strip())
        except ValueError:
            self.close_connection = True
            self._audit_webhook(hook_id, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, event_type)
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid content length"})
            return

        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._audit_webhook(hook_id, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, event_type)
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload too large"})
            return

        raw_body = self.rfile.read(length)
        if len(raw_body) < length:
            self.close_connection = True
            self._audit_webhook(hook_id, HTTPStatus.BAD_REQUEST, event_type)
            self._json(HTTPStatus.BAD_REQUEST, {"error": "incomplete body"})
            return

        # 3. Signature header X-Conveyor-Signature or GitHub's X-Hub-Signature-256
        sig_header = self.headers.get("X-Conveyor-Signature") or self.headers.get("X-Hub-Signature-256") or ""
        if not sig_header or not routines.verify_signature(hook["secret"], raw_body, sig_header):
            self._audit_webhook(hook_id, HTTPStatus.UNAUTHORIZED, event_type)
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid signature"})
            return

        # 4. Delivery id from X-Conveyor-Delivery or X-GitHub-Delivery (optional; max 200 chars).
        delivery_id = self.headers.get("X-Conveyor-Delivery") or self.headers.get("X-GitHub-Delivery")
        if delivery_id:
            delivery_id = delivery_id.strip()[:200]
            # Read-only here: a delivery rejected below (paused / busy) must stay
            # retryable with the same id. It is recorded only once accepted.
            if routines.delivery_seen(settings, hook_id, delivery_id):
                self._audit_webhook(hook_id, HTTPStatus.OK, event_type)
                self._json(HTTPStatus.OK, {"ok": True, "duplicate": True})
                return

        # 5. Routine paused (enabled=0) -> 409 {"error": "routine is paused"}.
        routine = routines.get_routine(settings, hook["routine_id"])
        if routine is None:
            self._audit_webhook(hook_id, HTTPStatus.NOT_FOUND, event_type)
            self._json(HTTPStatus.NOT_FOUND, {"error": "routine not found"})
            return

        if not routine.get("enabled", True):
            self._audit_webhook(hook_id, HTTPStatus.CONFLICT, event_type)
            self._json(HTTPStatus.CONFLICT, {"error": "routine is paused"})
            return

        # 6. Rate limit: at most one run in flight per routine and at most one accepted delivery per 10 seconds per hook -> 429 {"error": "busy"} with Retry-After.
        now = time.time()
        routine_id = int(routine["id"])
        with getattr(self.server, "_rate_limit_lock", threading.Lock()):
            active = getattr(self.server, "_active_routine_runs", None)
            if active is None:
                self.server._active_routine_runs = set()
                active = self.server._active_routine_runs
            last_accepted_map = getattr(self.server, "_hook_last_accepted", None)
            if last_accepted_map is None:
                self.server._hook_last_accepted = {}
                last_accepted_map = self.server._hook_last_accepted

            if routine_id in active:
                self._audit_webhook(hook_id, HTTPStatus.TOO_MANY_REQUESTS, event_type)
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "busy"}, extra_headers=[("Retry-After", "10")])
                return

            last_time = last_accepted_map.get(hook_id, 0.0)
            elapsed = now - last_time
            if elapsed < 10.0:
                retry_after = max(1, int(10.0 - elapsed + 0.999))
                self._audit_webhook(hook_id, HTTPStatus.TOO_MANY_REQUESTS, event_type)
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "busy"}, extra_headers=[("Retry-After", str(retry_after))])
                return

            if delivery_id and not routines.record_delivery(settings, hook_id, delivery_id):
                self._audit_webhook(hook_id, HTTPStatus.OK, event_type)
                self._json(HTTPStatus.OK, {"ok": True, "duplicate": True})
                return
            last_accepted_map[hook_id] = now
            active.add(routine_id)

        routines.record_hook_fired(settings, hook_id)

        # 7. Accept: 202 {"ok": true, "accepted": true} immediately; schedule the run on console loop
        formatted_payload = routines.format_webhook_payload(raw_body)
        event_dict = {"type": event_type, "payload": formatted_payload}

        runner = getattr(self.server.control, "runner", None)

        async def _do_run():
            try:
                await routines.run_single_routine(
                    settings, runner, routine, trigger="webhook", event=event_dict
                )
            except Exception:
                logger.exception("Webhook routine execution failed for routine #%d", routine_id)
            finally:
                with getattr(self.server, "_rate_limit_lock", threading.Lock()):
                    getattr(self.server, "_active_routine_runs", set()).discard(routine_id)

        asyncio.run_coroutine_threadsafe(_do_run(), self.server.loop)

        self._audit_webhook(hook_id, HTTPStatus.ACCEPTED, event_type)
        self._json(HTTPStatus.ACCEPTED, {"ok": True, "accepted": True})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = self._segments(parsed.path)

        # Webhook receiver: POST /hooks/<hook_id> (handled before /api/ auth check)
        if len(parts) == 2 and parts[0] == "hooks":
            self._handle_webhook(parts[1])
            return

        if not self._require_auth():
            return

        if not parsed.path.startswith("/api/"):
            self._drain_body()
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        body = self._body()
        if body is None:
            return
        try:
            if parsed.path == "/api/memory":
                settings = self._memory_settings()
                if settings is None:
                    return
                from personal_tools import long_term_memory as ltm
                raw_text = body.get("text", body.get("content"))
                if not isinstance(raw_text, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "text is required"})
                    return
                try:
                    # Same filter as chat writes: one sentence, length cap, secrets refused.
                    row = ltm.remember_fact(settings, ltm.WEB_OPERATOR, raw_text, source_channel="web")
                except ValueError as exc:
                    self._json(HTTPStatus.OK, {"ok": False, "refused": True, "error": str(exc)})
                    return
                resp = {"ok": True}
                resp.update(ltm.api_item(settings, row))
                self._json(HTTPStatus.CREATED, resp)
                return

            if parsed.path.startswith("/api/routines") or parsed.path.startswith("/api/inbox"):
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "routines_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)"})
                    return

                import routines
                if parsed.path == "/api/routines":
                    name = body.get("name")
                    schedule = body.get("schedule") or body.get("schedule_cron")
                    prompt = body.get("prompt")
                    deliver = body.get("deliver")
                    enabled = body.get("enabled", True)
                    try:
                        routine = routines.create_routine(
                            settings,
                            name=str(name or ""),
                            schedule=str(schedule or ""),
                            prompt=str(prompt or ""),
                            deliver=deliver,
                            enabled=bool(enabled),
                        )
                        self._json(HTTPStatus.CREATED, routine)
                    except ValueError as exc:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                elif len(parts) == 4 and parts[:2] == ["api", "routines"] and parts[3] == "hook":
                    if not (getattr(settings, "webhooks_enabled", False) and getattr(settings, "routines_enabled", False)):
                        self._json(HTTPStatus.CONFLICT, {"error": "webhooks are disabled (set CONVEYOR_WEBHOOKS_ENABLED=true and CONVEYOR_ROUTINES_ENABLED=true)"})
                        return
                    routine_id_str = parts[2]
                    if not routine_id_str.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid routine id"})
                        return
                    routine_id = int(routine_id_str)
                    try:
                        hook = routines.create_or_rotate_hook(settings, routine_id)
                        self._json(HTTPStatus.CREATED, {
                            "hook_id": hook["hook_id"],
                            "secret": hook["secret"],
                            "path": hook["path"],
                        })
                    except KeyError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "routine not found"})
                    return
                elif len(parts) == 4 and parts[:2] == ["api", "routines"] and parts[3] in ("pause", "resume", "run"):
                    routine_id_str = parts[2]
                    action = parts[3]
                    if not routine_id_str.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid routine id"})
                        return
                    routine_id = int(routine_id_str)
                    if action == "pause":
                        r = routines.pause_routine(settings, routine_id)
                        if not r:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "routine not found"})
                            return
                        self._json(HTTPStatus.OK, {"ok": True, "routine": r})
                    elif action == "resume":
                        r = routines.resume_routine(settings, routine_id)
                        if not r:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "routine not found"})
                            return
                        self._json(HTTPStatus.OK, {"ok": True, "routine": r})
                    elif action == "run":
                        try:
                            runner = getattr(self.server.control, "runner", None)
                            run_record = self._await(routines.run_routine_now(settings, routine_id, runner=runner), timeout=130)
                            self._json(HTTPStatus.OK, run_record)
                        except ValueError as exc:
                            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                        except Exception:
                            logger.exception("Routine run request failed")
                            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
                    return
                elif parsed.path == "/api/inbox/read-all":
                    marked = routines.mark_inbox_read_all(settings)
                    self._json(HTTPStatus.OK, {"ok": True, "marked": marked})
                    return
                elif len(parts) == 4 and parts[:2] == ["api", "inbox"] and parts[3] == "read":
                    run_id_str = parts[2]
                    if not run_id_str.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid run id"})
                        return
                    run_id = int(run_id_str)
                    ok = routines.mark_inbox_read(settings, run_id)
                    self._json(HTTPStatus.OK, {"ok": ok})
                    return

            if parsed.path == "/api/chat":
                raw_message = body.get("message")
                message = str(raw_message or "").strip() if isinstance(raw_message, str) else ""
                if not message or len(message) > 8_000:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "message must be 1-8000 characters"})
                    return
                requested_session_id = str(body.get("session_id") or "")
                from web_chat import WEB_CHAT_PREFIX, resolve_or_create_session
                session_info = resolve_or_create_session(
                    self.server.control, requested_session_id, new_prefix=WEB_CHAT_PREFIX,
                )
                if not session_info:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid session_id"})
                    return
                channel, operator_id, source_chat_id, durable_session_id = session_info
                if channel != "web":
                    # Tool confirmations from web chat are only decidable for
                    # web-channel sessions; don't act as a Telegram/Feishu chat.
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "web chat only supports web sessions"})
                    return

                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()

                from handlers.chat import chat_enabled
                if not chat_enabled(settings):
                    self._json(HTTPStatus.CONFLICT, {"error": "chat tier is disabled (set CONVEYOR_CHAT_MODE and CONVEYOR_CHAT_*)"})
                    return

                msg = InboundMessage(
                    channel=channel, operator_id=operator_id, chat_id=source_chat_id,
                    message_id=uuid.uuid4().hex, text=message, chat_type="p2p",
                )
                self._handle_chat_stream(msg, settings, durable_session_id, message)
                return
            elif parsed.path == "/api/tasks":
                prompt = str(body.get("prompt") or "").strip()
                if not prompt or len(prompt) > 8_000:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "prompt must be 1-8000 characters"})
                    return
                requested_session_id = str(body.get("session_id") or "")
                from web_chat import resolve_or_create_session
                session_info = resolve_or_create_session(self.server.control, requested_session_id)
                if not session_info:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid session_id"})
                    return
                channel, operator_id, source_chat_id, durable_session_id = session_info
                mode = JobMode.FIX if body.get("mode") == "fix" else JobMode.RUN
                msg = InboundMessage(
                    channel=channel, operator_id=operator_id, chat_id=source_chat_id,
                    message_id=uuid.uuid4().hex, text=prompt, chat_type="p2p",
                )
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                outbound = WebOutbound(
                    settings, durable_session_id,
                    is_codex=(mode == JobMode.FIX), prompt=prompt,
                )
                ok, message, job = self._await(self._handle_task(
                    msg, outbound, settings, self.server.control.runner, mode, prompt,
                ))
                self._json(HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT, {
                    "ok": ok, "message": message, "job_id": job.id if job else None,
                    "session_id": durable_session_id,
                })
            elif parsed.path == "/api/config/provider":
                result = self.server.control.update_provider_config(body)
                self._json(HTTPStatus.OK, {"ok": True, "config": result})
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] in ("archive", "delete"):
                if parts[3] == "delete":
                    ok = self.server.control.delete_session(parts[2])
                else:
                    ok = self.server.control.archive_session(parts[2])
                self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                ok, message = self._await(self.server.control.cancel_job(parts[2]))
                self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": message})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("apply", "discard"):
                approval = self.server.control.request_approval(parts[2], parts[3])
                self._json(HTTPStatus.ACCEPTED, {"approval": approval})
            elif len(parts) == 4 and parts[:2] == ["api", "approvals"] and parts[3] in ("approve", "reject"):
                approval_id = parts[2]
                is_approve = (parts[3] == "approve")
                from handlers.tools.confirm import get_pending
                pending = get_pending(approval_id)
                if pending is not None:
                    if pending.channel != "web":
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    settings = getattr(self.server.control, "settings", None)
                    if settings is None:
                        from config import load_runtime_settings
                        settings = load_runtime_settings()
                    from web_chat import decide_tool_approval
                    result = self._await(decide_tool_approval(pending, is_approve, settings), timeout=120)
                    self._json(HTTPStatus.OK, result)
                    return
                result = self._await(self.server.control.decide_approval(parts[2], is_approve), timeout=120)
                self._json(HTTPStatus.OK if result else HTTPStatus.NOT_FOUND, result or {"error": "not found"})
            elif len(parts) == 4 and parts[:2] == ["api", "approval-inbox"] and parts[3] in ("approve", "reject"):
                approval_id = parts[2]
                is_approve = (parts[3] == "approve")
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "approval_inbox_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "approval inbox is disabled (set CONVEYOR_APPROVAL_INBOX_ENABLED=true)"})
                    return

                from handlers.tools.confirm import get_pending
                pending = get_pending(approval_id)
                if pending is not None:
                    if pending.channel != "web":
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    if is_approve:
                        draft = body.get("draft") if isinstance(body, dict) else None
                        expected_arg = body.get("expected_arg") if isinstance(body, dict) else None
                        # Optimistic check against what the operator saw. The list shows the
                        # redacted arg, so the redacted form of the current arg also matches.
                        from redaction import redact_text as _redact
                        if expected_arg is not None and expected_arg not in (pending.arg, _redact(pending.arg)):
                            self._json(HTTPStatus.CONFLICT, {"error": "draft changed, reload"})
                            return
                        if draft is not None:
                            import approval_inbox
                            try:
                                pending = approval_inbox.edit_pending(settings, approval_id, draft)
                            except KeyError:
                                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                                return
                            except (ValueError, PermissionError) as exc:
                                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                                return
                        from web_chat import decide_tool_approval
                        result = self._await(decide_tool_approval(pending, True, settings), timeout=120)
                        if result.get("status") == "expired":
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                        self._json(HTTPStatus.OK, result)
                        return
                    else:
                        from web_chat import decide_tool_approval
                        result = self._await(decide_tool_approval(pending, False, settings), timeout=120)
                        if result.get("status") == "expired":
                            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                            return
                        self._json(HTTPStatus.OK, result)
                        return

                # Not a pending tool action; check job approval
                if is_approve:
                    if isinstance(body, dict) and "draft" in body and body.get("draft") is not None:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "job approvals do not support drafts"})
                        return
                    result = self._await(self.server.control.decide_approval(approval_id, True), timeout=120)
                    if result is None or result.get("status") == "expired":
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._json(HTTPStatus.OK, result)
                    return
                else:
                    result = self._await(self.server.control.decide_approval(approval_id, False), timeout=120)
                    if result is None or result.get("status") == "expired":
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    self._json(HTTPStatus.OK, result)
                    return
            elif parsed.path == "/api/computer/screenshot":
                result = self.server.control.request_host_screen()
                self._json(HTTPStatus.ACCEPTED if result.get("ok") else HTTPStatus.CONFLICT, result)
            elif parsed.path == "/api/computer/stop":
                result = self._await(self.server.control.emergency_stop())
                self._json(HTTPStatus.OK, {"ok": True, "result": result})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, TimeoutError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": redact_text(str(exc))})
        except Exception:
            logger.exception("POST request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self._require_auth():
            return
        if not parsed.path.startswith("/api/"):
            self._drain_body()
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._drain_body()
        parts = self._segments(parsed.path)
        try:
            if len(parts) == 3 and parts[:2] == ["api", "memory"]:
                settings = self._memory_settings()
                if settings is None:
                    return
                if not parts[2].isdigit():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid memory id"})
                    return
                from personal_tools import long_term_memory as ltm
                gone, status = ltm.forget_fact(settings, ltm.WEB_OPERATOR, f"#{parts[2]}")
                if status != "deleted":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "memory not found"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "deleted": ltm.api_item(settings, gone[0])})
                return

            if parsed.path.startswith("/api/routines"):
                settings = getattr(self.server.control, "settings", None)
                if settings is None:
                    from config import load_runtime_settings
                    settings = load_runtime_settings()
                if not getattr(settings, "routines_enabled", False):
                    self._json(HTTPStatus.CONFLICT, {"error": "routines are disabled (set CONVEYOR_ROUTINES_ENABLED=true)"})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "routines"] and parts[3] == "hook":
                    if not (getattr(settings, "webhooks_enabled", False) and getattr(settings, "routines_enabled", False)):
                        self._json(HTTPStatus.CONFLICT, {"error": "webhooks are disabled (set CONVEYOR_WEBHOOKS_ENABLED=true and CONVEYOR_ROUTINES_ENABLED=true)"})
                        return
                    routine_id_str = parts[2]
                    if not routine_id_str.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid routine id"})
                        return
                    routine_id = int(routine_id_str)
                    import routines
                    ok = routines.delete_hook(settings, routine_id)
                    self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": True} if ok else {"error": "hook not found"})
                    return
                if len(parts) == 3 and parts[:2] == ["api", "routines"]:
                    routine_id_str = parts[2]
                    if not routine_id_str.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid routine id"})
                        return
                    routine_id = int(routine_id_str)
                    import routines
                    ok = routines.delete_routine(settings, routine_id)
                    self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                    return

            if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                ok = self.server.control.archive_session(parts[2])
                self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception:
            logger.exception("DELETE request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def _stream_events(self, query: dict[str, list[str]]) -> None:
        job_id = str((query.get("job_id") or [""])[0])
        if not job_id or self.server.control.get_job(job_id) is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
            return
        try:
            sequence = max(0, int((query.get("after") or ["0"])[0]))
        except ValueError:
            sequence = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_heartbeat = 0.0
        deadline = time.monotonic() + 900
        try:
            while time.monotonic() < deadline:
                events = self.server.control.events(job_id, sequence, 200)
                for event in events:
                    sequence = max(sequence, int(event["sequence"]))
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {event['event_id']}\nevent: agent\ndata: {data}\n\n".encode("utf-8"))
                now = time.monotonic()
                if events or now - last_heartbeat >= 15:
                    if not events:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
                time.sleep(0.75)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_chat_stream(
        self,
        msg: InboundMessage,
        settings: Any,
        durable_session_id: str,
        prompt: str,
    ) -> None:
        import queue
        from web_chat import WebChatPort, run_web_chat

        event_queue: queue.Queue = queue.Queue()
        port = WebChatPort(event_queue, settings, durable_session_id, prompt=prompt)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        # Emit session event first
        session_data = json.dumps({"session_id": durable_session_id}, ensure_ascii=False)
        self.wfile.write(f"event: session\ndata: {session_data}\n\n".encode("utf-8"))
        self.wfile.flush()

        runner = getattr(self.server.control, "runner", None)
        future = asyncio.run_coroutine_threadsafe(
            run_web_chat(msg, port, settings, runner, prompt),
            self.server.loop,
        )

        deadline = time.monotonic() + 180.0
        done = False
        try:
            while not done:
                now = time.monotonic()
                if now > deadline:
                    future.cancel()
                    err_data = json.dumps({"error": "Request timed out"}, ensure_ascii=False)
                    done_data = json.dumps({"outcome": "timeout"}, ensure_ascii=False)
                    self.wfile.write(f"event: error\ndata: {err_data}\n\nevent: done\ndata: {done_data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break

                try:
                    event_name, data = event_queue.get(timeout=0.25)
                except queue.Empty:
                    if future.done():
                        exc = future.exception()
                        if exc is not None:
                            logger.error("Chat coroutine failed", exc_info=exc)
                            err_data = json.dumps({"error": "Internal error"}, ensure_ascii=False)
                            done_data = json.dumps({"outcome": "error"}, ensure_ascii=False)
                            self.wfile.write(f"event: error\ndata: {err_data}\n\nevent: done\ndata: {done_data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        break
                    continue

                payload = json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if event_name == "done":
                    done = True
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            future.cancel()
            logger.debug("Chat client disconnected: %s", self.client_address[0])

    def _static(self, path: str) -> None:
        relative = path.lstrip("/") or "index.html"
        candidate = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in candidate.parents and candidate != STATIC_ROOT.resolve():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not candidate.is_file():
            candidate = STATIC_ROOT / "index.html"
        if not candidate.is_file():
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "frontend not built"})
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._headers(HTTPStatus.OK, f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type, len(data))
        self.wfile.write(data)

    def _artifact(self, artifact_id: str) -> None:
        path = self.server.control.artifact_path(artifact_id)
        if path is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "artifact not found"})
            return
        data = path.read_bytes()
        self._headers(HTTPStatus.OK, "image/png", len(data))
        self.wfile.write(data)


def validate_web_config(settings: Any) -> None:
    if not settings.conveyor_web_enabled:
        raise RuntimeError("CONVEYOR_WEB_ENABLED is not true")
    token = settings.conveyor_web_token or ""
    if len(token) < 32:
        raise RuntimeError("CONVEYOR_WEB_TOKEN must contain at least 32 characters")
    if not (1 <= int(settings.conveyor_web_port) <= 65535):
        raise RuntimeError("CONVEYOR_WEB_PORT is invalid")


def validate_codex_bin(settings: Any) -> None:
    codex_bin = getattr(settings, "codex_bin", "") or ""
    if not codex_bin:
        raise RuntimeError(f"CODEX_BIN not found: {codex_bin}")
    resolved = None
    if os.path.sep in codex_bin or (os.path.altsep and os.path.altsep in codex_bin):
        path = Path(codex_bin).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            resolved = str(path)
    else:
        resolved = shutil.which(codex_bin)
    if not resolved:
        raise RuntimeError(f"CODEX_BIN not found: {codex_bin}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Conveyor Web Console")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    args = parser.parse_args()
    configure_logging(
        service_name="conveyor.web",
        level=logging.INFO,
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_runtime_settings()
    validate_web_config(settings)
    if args.check:
        try:
            validate_codex_bin(settings)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        print("web console configuration: ok")
        return
    runner = CodexRunner(settings)
    queue = get_job_queue()
    # The chat worker owns restart recovery. Merely starting the Web Console
    # must never relabel a live Telegram/Feishu job as interrupted.
    queue.configure(settings, runner, recover=False)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    control = WebControl(settings, runner, queue)
    server = WebConsoleServer(
        (settings.conveyor_web_host, settings.conveyor_web_port),
        WebConsoleHandler,
        control=control,
        loop=loop,
        token=settings.conveyor_web_token,
    )
    thread = threading.Thread(target=server.serve_forever, name="conveyor-web-http", daemon=True)
    thread.start()
    logger.info("Conveyor Web Console listening on http://%s:%d", settings.conveyor_web_host, settings.conveyor_web_port)

    import routines
    routines.start_routines_worker(loop, settings, runner)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        loop.stop()
        loop.close()


if __name__ == "__main__":
    main()
