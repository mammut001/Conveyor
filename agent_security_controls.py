"""Authenticated runtime Agent kill switch controls.

No model call, channel binding or message content grants switch access.
"""
from __future__ import annotations

from http import HTTPStatus
from urllib.parse import urlparse

import agent_security


def available(settings):
    """Keep emergency unkill accessible, but not without Agent features."""
    import named_agents
    return named_agents.enabled(settings)


def web_post(handler, path, body):
    if path != "/api/agents/kill":
        return False
    if not available(getattr(handler.server.control, "settings", None)):
        handler._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        return True
    # Caller is behind the Web Console's existing _require_auth() bearer gate.
    origin = handler.headers.get("Origin", "")
    host = handler.headers.get("Host", "")
    fetch_site = handler.headers.get("Sec-Fetch-Site", "")
    parsed = urlparse(origin) if origin else None
    if (fetch_site in ("cross-site", "same-site")
        or (origin and (parsed.scheme not in ("http", "https")
                        or parsed.netloc.lower() != host.lower()))
        or handler.headers.get("X-Requested-With") != "XMLHttpRequest"
        or "application/json" not in handler.headers.get(
            "Content-Type", "").lower()):
        handler._json(HTTPStatus.FORBIDDEN, {"error": "origin or request denied"})
        return True
    if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
        handler._json(HTTPStatus.BAD_REQUEST, {"error": "enabled boolean required"})
        return True
    settings = handler.server.control.settings
    if settings is None:
        handler._json(HTTPStatus.CONFLICT, {"error": "settings unavailable"})
        return True
    try:
        active = agent_security.set_kill(
            settings, enabled=body["enabled"], operator="web-console",
            source="web")
        if active:
            # Cancel leases immediately, not only at next worker poll.
            from agent_delegation_worker import cancel_all
            cancel_all(settings)
    except OSError:
        handler._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                      {"error": "cannot change runtime kill"})
        return True
    handler._json(HTTPStatus.OK, {"killed": active,
                                  "env_override": bool(active and not body["enabled"])})
    return True


async def owner_command(settings, msg, port):
    """The explicit /agentkill on|off command, only in verified owner DMs."""
    token, _, tail = msg.text.strip().partition(" ")
    if token.split("@", 1)[0].lower() != "/agentkill":
        return False
    from channel.auth import is_allowed
    if msg.channel not in ("telegram", "feishu") or msg.chat_type != "p2p":
        return True
    expected = (str(settings.telegram_allowed_user_id) if msg.channel == "telegram"
                else str(settings.lark_allowed_open_id or ""))
    if not expected or msg.operator_id != expected or not is_allowed(msg, settings):
        return True  # Silent for unauthorized users, including when killed.
    desired = tail.strip().lower()
    if desired not in ("on", "off"):
        await port.reply(msg, "Usage: /agentkill on | /agentkill off")
        return True
    actual = agent_security.set_kill(
        settings, enabled=desired == "on",
        operator=msg.operator_id, source=msg.channel)
    if actual:
        from agent_delegation_worker import cancel_all
        cancel_all(settings)
    await port.reply(msg, "[Agent safety] Kill switch " + (
        "ON (delegations stopped)" if actual else "OFF (delegations resumed)"))
    return True
