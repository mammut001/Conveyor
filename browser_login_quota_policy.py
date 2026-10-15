"""Separate credential-rejection lockout from bounded infrastructure retry abuse.

Only a verified POST and rejected password consumes the three-per-hour quota.
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
    return result == "failed" and reason == "credentials_rejected" and bool(submitted)


def admission_reason(rows, now):
    """Rows: (result, reason, submitted, timestamp), newest first."""
    rejected = sum(is_rejection(r, why, submitted) and when > now - REJECT_WINDOW
                   for r, why, submitted, when in rows)
    if rejected >= REJECT_LIMIT:
        return "locked"
    # In-flight reservations count briefly so different server processes
    # cannot all race past the remaining credential-attempt budget.
    pending = sum(r == "started" and when > now - INFLIGHT_WINDOW
                  for r, _why, _submitted, when in rows)
    if rejected + pending >= REJECT_LIMIT:
        return "rate_limited"
    infra = sum(r == "failed" and not is_rejection(r, why, submitted)
                and when > now - INFRA_WINDOW
                for r, why, submitted, when in rows)
    if infra >= INFRA_LIMIT:
        return "infra_rate_limited"
    return None
