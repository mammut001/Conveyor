"""Authenticated-only read/revoke API for G4. No session-creation HTTP route."""
from __future__ import annotations
from browser_auth_sessions import manager_for
from browser_auth_policy import enabled
from workflow_browser_external_identity import ExternalPolicyDenied


def get(handler, path):
    if path != "/api/browser/auth-sessions":
        return False
    if not enabled():
        handler._json(409, {"error": "g4_disabled"})
        return True
    try:
        sessions = handler._await(manager_for(handler.server.control.settings).list_public())
        handler._json(200, {"sessions": sessions})
    except Exception:
        handler._json(409, {"error": "g4_unavailable"})
    return True


def post(handler, path, body):
    prefix = "/api/browser/auth-sessions/"
    if not (path.startswith(prefix) and path.endswith("/revoke")):
        return False
    if not enabled():
        handler._json(409, {"error": "g4_disabled"})
        return True
    session_id = path[len(prefix):-len("/revoke")]
    if (len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id)
            or body):
        handler._json(400, {"error": "invalid session id or body"})
        return True
    try:
        removed = handler._await(manager_for(handler.server.control.settings).revoke(session_id))
        handler._json(200 if removed else 404, {"ok": removed})
    except Exception:
        handler._json(409, {"error": "g4_revoke_failed"})
    return True
