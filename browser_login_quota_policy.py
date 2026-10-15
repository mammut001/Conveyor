"""Separate credential-rejection lockout from bounded infrastructure retry abuse.

Any dispatched login POST with a non-success outcome consumes the three-per-hour quota.
A short per-domain infrastructure cap prevents unlimited launch/navigation
hammering without treating those failures as wrong passwords.
"""
from __future__ import annotations

REJECT_LIMIT = 3
REJECT_WINDOW = 3600
INFRA_LIMIT = 5
INFRA_WINDOW = 600
INFLIGHT_WINDOW = 120


def is_rejection(result, reason, submitted):
    # Preserve already-established lockouts from pre-migration databases.
    # A dispatched POST may have sent a wrong password even when the response
    # was 5xx, redirect, timed out, or the process crashed before finishing.
    return ((result == "failed" and reason == "legacy_unverified")
            or (bool(submitted) and result in ("started", "failed")))


def admission_reason(rows, now):
    """Rows: (result, reason, submitted, timestamp), newest first."""
    rejected = sum(is_rejection(r, why, submitted) and when > now - REJECT_WINDOW
                   for r, why, submitted, when in rows)
    if rejected >= REJECT_LIMIT:
        return "locked"
    # Fresh, not-yet-dispatched reservations protect against parallel requests.
    # Already-dispatched 'started' rows are included in rejected above.
    pending = sum(r == "started" and not submitted
                  and when > now - INFLIGHT_WINDOW
                  for r, _why, submitted, when in rows)
    if rejected + pending >= REJECT_LIMIT:
        return "rate_limited"
    # Crashed pre-submit rows stop reserving capacity after 120s, but are
    # still infrastructure failures within their own ten-minute window.
    infra = sum(not is_rejection(r, why, submitted)
                and why != "busy"
                and ((r == "failed" and why != "legacy_unverified") or
                     (r == "started" and when <= now - INFLIGHT_WINDOW))
                and when > now - INFRA_WINDOW
                for r, why, submitted, when in rows)
    if infra >= INFRA_LIMIT:
        return "infra_rate_limited"
    return None
