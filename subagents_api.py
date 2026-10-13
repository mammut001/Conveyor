"""Authenticated web console adapter for background subagents.

The caller must have passed WebConsoleHandler._require_auth first.
"""
from http import HTTPStatus
import subagents


def get(handler, path: str) -> bool:
    if path != "/api/subagents":
        return False
    settings = getattr(handler.server.control, "settings", None)
    if not settings or not subagents.enabled(settings):
        handler._json(HTTPStatus.CONFLICT, {"error": "subagents disabled"})
    else:
        handler._json(HTTPStatus.OK, {"children": subagents.list_children(settings)})
    return True


def post(handler, parts: list[str]) -> bool:
    is_kill = len(parts) == 3 and parts == ["api", "subagents", "kill"]
    is_cancel = len(parts) == 4 and parts[:2] == ["api", "subagents"] and parts[3] == "cancel"
    if not (is_kill or is_cancel):
        return False
    settings = getattr(handler.server.control, "settings", None)
    if not settings or not subagents.enabled(settings):
        handler._json(HTTPStatus.CONFLICT, {"error": "subagents disabled"})
    elif is_kill:
        handler._json(HTTPStatus.OK, {"cancelled": subagents.kill_all(settings)})
    elif subagents.cancel(settings, parts[2]):
        handler._json(HTTPStatus.OK, {"cancelled": True})
    else:
        handler._json(HTTPStatus.NOT_FOUND, {"error": "child not found or not active"})
    return True
