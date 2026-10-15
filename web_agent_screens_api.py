"""Small authenticated Web Console adapter for per-Agent screen data."""
from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import web_agent_screens as screens


def _settings(handler):
    return getattr(handler.server.control, "settings", None)


def get(handler, path, query):
    if not path.startswith("/api/agent-screens"):
        return False
    settings = _settings(handler)
    if not screens.enabled(settings):
        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    if path == "/api/agent-screens/config":
        handler._json(HTTPStatus.OK, screens.features(settings))
        return True
    if path != "/api/agent-screens":
        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    agent_id = (query.get("agent_id") or [""])[0]
    view = (query.get("view") or [""])[0]
    if not agent_id or not view:
        handler._json(HTTPStatus.BAD_REQUEST, {"error": "agent_id and view required"})
        return True
    try:
        items = screens.list_items(settings, agent_id, view)
    except (ValueError, PermissionError, LookupError):
        handler._json(HTTPStatus.NOT_FOUND, {"error": "agent screen unavailable"})
        return True
    handler._json(HTTPStatus.OK, {"agent_id": agent_id, "items": items})
    return True


def post(handler, path, body):
    if path != "/api/agent-screens/cancel":
        return False
    settings = _settings(handler)
    if not screens.enabled(settings) or not screens.features(settings)["tasks"]:
        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    # Console uses bearer headers, never ambient cookies. Add an explicit
    # same-origin/fetch-metadata defense for this one mutating operation.
    origin = handler.headers.get("Origin", "")
    host = handler.headers.get("Host", "")
    if (handler.headers.get("Sec-Fetch-Site", "") in ("cross-site", "same-site")
        or (origin and (urlparse(origin).hostname or "").lower() !=
            host.split(":")[0].lower())
        or handler.headers.get("X-Requested-With") != "XMLHttpRequest"
        or "application/json" not in handler.headers.get("Content-Type", "").lower()):
        handler._json(HTTPStatus.FORBIDDEN, {"error": "origin or request denied"})
        return True
    if not isinstance(body.get("agent_id"), str) or not isinstance(body.get("task_id"), str):
        handler._json(HTTPStatus.BAD_REQUEST, {"error": "agent_id and task_id required"})
        return True
    try:
        cancelled = screens.cancel_task(settings, body["agent_id"], body["task_id"])
    except (ValueError, LookupError, PermissionError):
        handler._json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
        return True
    handler._json(HTTPStatus.OK, {"cancelled": cancelled})
    return True
