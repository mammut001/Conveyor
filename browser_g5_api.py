"""G5 authenticated Web console status and emergency per-domain revocation.

Routes are absent (404) unless G5 is explicitly enabled. There is no
'approve' or 'promote' route: manifests require separate off-line review.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import browser_g5_rollout as g5
from workflow_browser_external_identity import ExternalPolicyDenied


def _settings(handler):
    from config import load_runtime_settings
    return getattr(handler.server.control, "settings", None) or load_runtime_settings()


def _root(settings):
    return Path(settings.codex_memory_root) / "browser_external"


def get(handler, path):
    if not g5.enabled() or path not in (
        "/api/browser/g5/status", "/api/browser/g5/metrics"):
        return False
    settings = _settings(handler)
    try:
        state = g5.status(_root(settings))
        if path.endswith("/metrics"):
            # Already-redacted G1/G2 egress records. No URLs or user content.
            from browser_approvals_admin import egress
            rows = egress(settings, 200)
            denied = sum(r["outcome"] == "denied" for r in rows)
            reasons = {}
            for item in rows:
                if item["outcome"] == "denied":
                    reason = item["reason"]
                    reasons[reason] = reasons.get(reason, 0) + 1
            state = {"recent_egress_count": len(rows), "denied": denied,
                     "denial_reasons": reasons,
                     "alert": bool(denied >= 5 or any(
                         r in reasons for r in ("audit_storage_unavailable",
                                                "request_filter_error",
                                                "dns_private_address_denied"))),
                     "alert_channel": "authenticated_web_console",
                     "auto_promotion": False}
        handler._json(200, state)
    except (ExternalPolicyDenied, OSError, ValueError, Exception):
        handler._json(409, {"error": "g5_status_unavailable"})
    return True


def post(handler, path, body):
    if not g5.enabled() or path != "/api/browser/g5/revoke":
        return False
    if not isinstance(body, dict) or set(body) != {"origin"}:
        handler._json(400, {"error": "expected origin"})
        return True
    settings = _settings(handler)
    try:
        domain = g5.revoke(_root(settings), body["origin"])
    except (ExternalPolicyDenied, OSError, ValueError):
        handler._json(400, {"error": "g5_invalid_origin_or_storage"})
        return True

    # Revoke is durable BEFORE closing Chromium. A crash or timeout leaves
    # every subsequent check denied; session sweep finishes the teardown.
    async def _close_domain():
        from browser_auth_sessions import manager_for
        manager = manager_for(settings)
        matches = [(s.id, s.operator) for s in list(manager.sessions.values())
                   if s.domain.rstrip("/") == domain]
        await asyncio.wait_for(
            asyncio.gather(*(manager.revoke(sid, operator=operator)
                             for sid, operator in matches)), timeout=8)

    try:
        handler._await(_close_domain(), timeout=10)
        handler._json(200, {"ok": True, "origin": domain,
                            "pending_cleanup": False})
    except Exception:
        handler._json(202, {"ok": True, "origin": domain,
                            "pending_cleanup": True})
    return True
