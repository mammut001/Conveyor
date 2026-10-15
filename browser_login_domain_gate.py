"""Atomic per-domain admission for opt-in automated browser login.

The reservation occurs synchronously before the first await in G4 open(), so
concurrent login calls cannot both launch Chromium for the same origin.
"""
from __future__ import annotations

from contextlib import contextmanager
from workflow_browser_external_identity import ExternalPolicyDenied


def login_domain_busy(manager, domain):
    return (domain in getattr(manager, "_opening_login_domains", set())
            or domain in getattr(manager, "_closing_domains", {}).values()
            or any(not session._closed and session.domain == domain
                   for session in manager.sessions.values()))


@contextmanager
def reserve_login_domain(manager, domain):
    if login_domain_busy(manager, domain):
        raise ExternalPolicyDenied("login_domain_busy")
    pending = manager.__dict__.setdefault("_opening_login_domains", set())
    pending.add(domain)
    try:
        yield
    finally:
        pending.discard(domain)
