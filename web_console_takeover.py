#!/usr/bin/env python3
"""Web Console entrypoint with Secure Human Takeover API routes.

Kept as a thin extension of ``web_console.py`` so the existing task/session/SSE
surface remains unchanged. Only authenticated ``/api/takeover/*`` routes are
added here.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from config import load_runtime_settings
from handlers.job_queue import get_job_queue
from logging_setup import configure_logging
from redaction import redact_text
from runner import CodexRunner
from web_console import (
    WebConsoleHandler,
    WebConsoleServer,
    start_extra_listeners,
    validate_codex_bin,
    validate_web_config,
)
from web_control import WebControl
from web_takeover import WebTakeover

logger = logging.getLogger("conveyor.web")


class TakeoverWebConsoleServer(WebConsoleServer):
    def __init__(self, *args: Any, takeover: WebTakeover, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.takeover = takeover


class TakeoverWebConsoleHandler(WebConsoleHandler):
    server: TakeoverWebConsoleServer

    def _takeover_enabled(self) -> bool:
        takeover = getattr(self.server, "takeover", None)
        if takeover is not None:
            enabled = getattr(takeover, "enabled", None)
            if enabled is not None:
                return bool(enabled() if callable(enabled) else enabled)
            settings = getattr(takeover, "settings", None)
            if settings is not None:
                return bool(getattr(settings, "conveyor_takeover_enabled", False))
        control = getattr(self.server, "control", None)
        if control is not None:
            settings = getattr(control, "settings", None)
            if settings is not None:
                return bool(getattr(settings, "conveyor_takeover_enabled", False))
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/takeover/status":
            super().do_GET()
            return
        if not self._require_auth():
            return
        if not self._takeover_enabled():
            self._json(
                HTTPStatus.OK,
                {
                    "enabled": False,
                    "takeover": None,
                    "privacy_mode": False,
                    "closing": None,
                    "transport_allowed": False,
                    "transport": None,
                    "message": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)",
                },
            )
            return
        try:
            status = self.server.takeover.status()
            if isinstance(status, dict) and "enabled" not in status:
                status["enabled"] = True
            self._json(HTTPStatus.OK, status)
        except Exception:
            logger.exception("Takeover status request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/takeover/"):
            super().do_POST()
            return
        if not self._require_auth():
            return
        if not self._takeover_enabled():
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)"},
            )
            return
        body = self._body()
        if body is None:
            return
        try:
            if path == "/api/takeover/start":
                result = self.server.takeover.start(body)
                self._json(HTTPStatus.ACCEPTED, result)
            elif path == "/api/takeover/activate":
                result = self.server.takeover.activate(str(body.get("session_id") or ""))
                self._json(HTTPStatus.OK, result)
            elif path in {"/api/takeover/complete", "/api/takeover/cancel"}:
                action = "complete" if path.endswith("/complete") else "cancel"
                result = self.server.takeover.close(str(body.get("session_id") or ""), action)
                self._json(HTTPStatus.ACCEPTED, result)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except RuntimeError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": redact_text(str(exc))})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": redact_text(str(exc))})
        except Exception:
            logger.exception("Takeover mutation failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Conveyor Web Workbench")
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

    logger.info(
        "Human takeover is %s",
        "enabled" if settings.conveyor_takeover_enabled else "disabled",
    )

    runner = CodexRunner(settings)
    queue = get_job_queue()
    queue.configure(settings, runner, recover=False)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    control = WebControl(settings, runner, queue)
    takeover = WebTakeover(settings)
    server = TakeoverWebConsoleServer(
        (settings.conveyor_web_host, settings.conveyor_web_port),
        TakeoverWebConsoleHandler,
        control=control,
        loop=loop,
        token=settings.conveyor_web_token,
        takeover=takeover,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="conveyor-web-http",
        daemon=True,
    )
    thread.start()
    import routines
    routines.start_routines_worker(loop, settings, runner)
    import approval_relay
    relay_consumer = approval_relay.start_relay_worker(loop, settings, channel="web")
    logger.info(
        "Conveyor Web Workbench listening on http://%s:%d",
        settings.conveyor_web_host,
        settings.conveyor_web_port,
    )
    start_extra_listeners(server, settings)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if relay_consumer is not None:
            relay_consumer.stop()
        server.shutdown()
        server.server_close()
        loop.stop()
        loop.close()


if __name__ == "__main__":
    main()
