"""Authenticated-only create/list/revoke API for G4, with no agent-facing routes."""
from __future__ import annotations
from browser_auth_sessions import manager_for
from browser_auth_policy import enabled
from workflow_browser_external_identity import ExternalPolicyDenied


def get(handler, path):
    if path != "/api/browser/auth-sessions":
        return False
    if not enabled():
        # The pre-G4 Web route owns the identical 404 response.
        return False
    try:
        sessions = handler._await(manager_for(handler.server.control.settings).list_public(operator="web-console"))
        handler._json(200, {"sessions": sessions})
    except Exception:
        handler._json(409, {"error": "g4_unavailable"})
    return True


def post(handler, path, body):
    prefix = "/api/browser/auth-sessions/"
    if path != "/api/browser/auth-sessions" and not (
        path.startswith(prefix) and path.endswith("/revoke")
    ):
        return False
    if not enabled():
        # The pre-G4 Web route owns the identical 404 response.
        return False
    if path == "/api/browser/auth-sessions":
        if set(body) != {"task_id", "domain"}:
            handler._json(400, {"error": "expected task_id and domain"})
            return True
        try:
            # One Web bearer operator; cannot be selected by the request.
            session = handler._await(manager_for(handler.server.control.settings).open(
                "web-console", body["task_id"], body["domain"]))
            handler._json(201, {"id": session.id, "domain": session.domain,
                                "task_id": session.task, "expires_at": session.expires_at})
        except ExternalPolicyDenied as exc:
            handler._json(409, {"error": str(exc)})
        except Exception:
            handler._json(409, {"error": "g4_start_denied"})
        return True
    session_id = path[len(prefix):-len("/revoke")]
    if (len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id)
            or body):
        handler._json(400, {"error": "invalid session id or body"})
        return True
    try:
        removed = handler._await(manager_for(handler.server.control.settings).revoke(session_id, operator="web-console"))
        handler._json(200 if removed else 404, {"ok": removed})
    except Exception:
        handler._json(409, {"error": "g4_revoke_failed"})
    return True
