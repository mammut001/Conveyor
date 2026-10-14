"""G4 explicit origin policy. Never import or inspect an existing user profile."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from workflow_browser_external_identity import (
    ExternalPolicyDenied, canonical_host, resolve_public_ipv4,
)


def enabled() -> bool:
    return os.getenv("CONVEYOR_BROWSER_AUTH_SESSIONS", "") == "1"


def ttl() -> int:
    try:
        value = int(os.getenv("CONVEYOR_BROWSER_AUTH_TTL_SEC", "900"))
    except (TypeError, ValueError):
        raise ExternalPolicyDenied("g4_invalid_ttl") from None
    if not 30 <= value <= 1800:
        raise ExternalPolicyDenied("g4_invalid_ttl")
    return value


def _origin(value: str) -> str:
    """Exact scheme/host/port. Loopback HTTP is only a fixture test opt-in."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 280:
        raise ExternalPolicyDenied("g4_origin_denied")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ExternalPolicyDenied("g4_origin_denied") from None
    if (parsed.path or parsed.query or parsed.fragment or parsed.username
            or parsed.password or not parsed.hostname or not port or
            not 1 <= port <= 65535 or value != f"{parsed.scheme}://{parsed.netloc}"):
        raise ExternalPolicyDenied("g4_origin_denied")
    if parsed.hostname == "127.0.0.1" and parsed.scheme == "http":
        if os.getenv("CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK") != "1":
            raise ExternalPolicyDenied("g4_loopback_disabled")
    elif parsed.scheme == "https":
        canonical_host(parsed.hostname)
        if parsed.netloc != f"{parsed.hostname}:{port}":
            raise ExternalPolicyDenied("g4_origin_denied")
    else:
        raise ExternalPolicyDenied("g4_origin_denied")
    return value


def origins() -> frozenset[str]:
    raw = os.getenv("CONVEYOR_BROWSER_AUTH_DOMAINS", "")
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if len(values) > 16:
        raise ExternalPolicyDenied("g4_domain_budget")
    return frozenset(_origin(v) for v in values)


def assets() -> frozenset[str]:
    raw = os.getenv("CONVEYOR_BROWSER_AUTH_ASSET_DOMAINS", "")
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if len(values) > 16:
        raise ExternalPolicyDenied("g4_asset_budget")
    return frozenset(_origin(v) for v in values)


def check_request(url: str, method: str, domain: str, lease_active: bool) -> None:
    """No external IdP, off-domain navigation, or unapproved form submission."""
    try:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        _origin(origin)
    except (ValueError, ExternalPolicyDenied):
        raise ExternalPolicyDenied("g4_off_domain") from None
    if origin == domain:
        if method in ("GET", "HEAD"):
            return
        # A login is human-driven within the protected lease, never by agent.
        if (method == "POST" and lease_active and
                parsed.path in ("/login", "/mfa", "/signin", "/session")):
            return
        raise ExternalPolicyDenied("g4_g3_approval_required")
    if origin in assets() and method in ("GET", "HEAD"):
        return
    raise ExternalPolicyDenied("g4_off_domain")


def browser_pins(domain: str) -> list[str]:
    """Resolve public hosts once; deny all names other than pinned sites."""
    hosts = sorted({urlsplit(x).hostname for x in (domain, *assets())})
    mapping = []
    for host in hosts:
        if host == "127.0.0.1":
            mapping.append("MAP 127.0.0.1 127.0.0.1")
        else:
            origin = next(x for x in (domain, *assets()) if urlsplit(x).hostname == host)
            ip = resolve_public_ipv4(host, urlsplit(origin).port)
            mapping.append(f"MAP {host} {ip}")
    return [f"--host-resolver-rules={', '.join(mapping)}, MAP * ~NOTFOUND"]
