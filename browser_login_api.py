"""Owner-only browser login entry API. Values are never returned.

Mounted only after normal Web Bearer authentication AND the explicit flag.
"""
from __future__ import annotations
from urllib.parse import parse_qs, urlsplit

from browser_login_credentials import enabled, put, public_list, remove
from workflow_browser_external_identity import ExternalPolicyDenied
from console_secrets import SecretStoreError


def get(handler, path):
    if path != "/api/browser/logins" or not enabled():
        return False
    try:
        handler._json(200, {"logins": public_list(handler.server.control.settings)})
    except Exception:
        handler._json(409, {"error": "login_store_unavailable"})
    return True


def post(handler, path, body):
    if path != "/api/browser/logins" or not enabled():
        return False
    if (not isinstance(body, dict) or
            not {"domain", "username", "password"} <= set(body) or
            set(body) - {"domain", "username", "password", "login_url"}):
        handler._json(400, {"error": "invalid_login_entry"})
        return True
    try:
        record = put(handler.server.control.settings, body["domain"],
                     body["username"], body["password"], body.get("login_url"))
        handler._json(201, record)
    except (ExternalPolicyDenied, SecretStoreError):
        handler._json(400, {"error": "invalid_login_entry"})
    except Exception:
        handler._json(409, {"error": "login_store_unavailable"})
    return True


def delete(handler, path, query):
    if path != "/api/browser/logins" or not enabled():
        return False
    parsed = parse_qs(query, keep_blank_values=True)
    if set(parsed) != {"domain"} or len(parsed["domain"]) != 1:
        handler._json(400, {"error": "invalid_login_domain"})
        return True
    try:
        removed = remove(handler.server.control.settings, parsed["domain"][0])
        handler._json(200 if removed else 404,
                      {"ok": True} if removed else {"error": "not found"})
    except (ExternalPolicyDenied, SecretStoreError):
        handler._json(400, {"error": "invalid_login_domain"})
    except Exception:
        handler._json(409, {"error": "login_store_unavailable"})
    return True
