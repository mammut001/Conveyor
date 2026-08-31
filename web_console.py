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
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from channel.types import InboundMessage
from config import load_settings
from handlers.job_queue import get_job_queue
from handlers.jobs import submit_codex_job
from redaction import SecretRedactingFilter, redact_text
from runner import CodexRunner, JobMode
from web_control import WebControl

logger = logging.getLogger("conveyor.web")
MAX_BODY_BYTES = 65_536
STATIC_ROOT = Path(__file__).resolve().parent / "web" / "dist"


class WebOutbound:
    """Browser rendering is event-driven, so chat replies are no-ops."""

    supports_inline_buttons = False

    async def reply(self, _msg: InboundMessage, _text: str) -> str | None:
        return "web-event"

    async def send_new(self, _msg: InboundMessage, _text: str) -> str | None:
        return "web-event"

    async def edit_progress(self, _msg: InboundMessage, _placeholder_id: str, _text: str) -> bool:
        return True

    async def reply_with_buttons(self, _msg: InboundMessage, _text: str, _buttons: list[list[dict]]) -> str | None:
        return "web-event"

    async def send_image(self, _chat_id: str, _image_path: str, *, caption: str | None = None) -> None:
        return None


class WebConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *,
                 control: WebControl, loop: asyncio.AbstractEventLoop, token: str) -> None:
        super().__init__(address, handler)
        self.control = control
        self.loop = loop
        self.token = token


class WebConsoleHandler(BaseHTTPRequestHandler):
    server_version = "Conveyor"
    sys_version = ""
    server: WebConsoleServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.client_address[0], fmt % args)

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "public, max-age=300")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = header[len(prefix):] if header.startswith(prefix) else ""
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/") or not self._require_auth():
            return
        parts = self._segments(parsed.path)
        body = self._body()
        if body is None:
            return
        try:
            if parsed.path == "/api/tasks":
                prompt = str(body.get("prompt") or "").strip()
                if not prompt or len(prompt) > 8_000:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "prompt must be 1-8000 characters"})
                    return
                session_id = str(body.get("session_id") or f"web-{uuid.uuid4().hex[:12]}")
                if len(session_id) > 128 or not all(ch.isalnum() or ch in "-_" for ch in session_id):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid session_id"})
                    return
                mode = JobMode.FIX if body.get("mode") == "fix" else JobMode.RUN
                msg = InboundMessage(
                    channel="web", operator_id="web-console", chat_id=session_id,
                    message_id=uuid.uuid4().hex, text=prompt, chat_type="p2p",
                )
                ok, message, job = self._await(submit_codex_job(
                    msg, WebOutbound(), self.server.control.runner,
                    mode=mode, prompt=prompt, wait=False,
                ))
                self._json(HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT, {
                    "ok": ok, "message": message, "job_id": job.id if job else None,
                    "session_id": session_id,
                })
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                ok, message = self._await(self.server.control.cancel_job(parts[2]))
                self._json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": message})
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("apply", "discard"):
                approval = self.server.control.request_approval(parts[2], parts[3])
                self._json(HTTPStatus.ACCEPTED, {"approval": approval})
            elif len(parts) == 4 and parts[:2] == ["api", "approvals"] and parts[3] in ("approve", "reject"):
                result = self._await(self.server.control.decide_approval(parts[2], parts[3] == "approve"), timeout=120)
                self._json(HTTPStatus.OK if result else HTTPStatus.NOT_FOUND, result or {"error": "not found"})
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Conveyor Web Console")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactingFilter())
    settings = load_settings()
    validate_web_config(settings)
    if args.check:
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
