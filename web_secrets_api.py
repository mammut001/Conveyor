"""Authenticated-only secrets endpoint dispatcher for Web Console."""
from __future__ import annotations

from http import HTTPStatus

from console_secrets import SecretStoreError, delete_secret, list_secrets, set_secret


def dispatch(handler, method: str, path: str, body=None) -> bool:
    if path != "/api/secrets" and not path.startswith("/api/secrets/"):
        return False
    # Dispatch occurs only after WebConsoleHandler._require_auth.
    settings = handler.server.control.settings
    try:
        if method == "GET" and path == "/api/secrets":
            handler._json(HTTPStatus.OK, {"items": list_secrets(settings)})
        elif method == "POST" and path == "/api/secrets":
            if not isinstance(body, dict) or set(body) != {"name", "value"}:
                raise SecretStoreError("name and value required")
            result = set_secret(settings, body["name"], body["value"])
            handler._json(HTTPStatus.CREATED, result)
        elif method == "DELETE" and path.startswith("/api/secrets/"):
            # Do not decode percent-escaped separators into additional paths.
            from urllib.parse import unquote
            raw = path[len("/api/secrets/"):]
            if not raw or "/" in raw or "%" in unquote(raw):
                raise SecretStoreError("invalid secret name")
            deleted = delete_secret(settings, unquote(raw))
            handler._json(HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND,
                          {"ok": True} if deleted else {"error": "not found"})
        else:
            handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
    except SecretStoreError as exc:
        handler._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    except (OSError, ValueError):
        # Do not echo exception messages: paths and values are private.
        handler._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "secret store unavailable"})
    return True
