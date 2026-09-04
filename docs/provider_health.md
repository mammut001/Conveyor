# Provider health and circuit breaker

Conveyor records provider health in the same SQLite control-plane database used by the queue, runtime ownership, events and transcripts.

The health record is keyed by provider ID and scoped to a non-secret configuration revision. It stores only bounded/redacted failure state; API keys and raw provider envelopes are not stored in the health table or returned by the Web API.

## States

- `unknown` — no result has been observed for the current configuration
- `healthy` — the latest provider attempt completed successfully
- `rate_limited` — the provider reported 429 / high-demand / overload behavior
- `auth_failed` — the provider rejected authentication/authorization
- `unreachable` — timeout or network-connectivity failure
- `error` — another provider attempt failure that does not open the circuit by default
- `recovering` — a prior circuit cooldown expired and the next task may probe the provider again

## Fail-fast behavior

For provider-specific rate-limit, authentication or connectivity failures, the default circuit threshold is one failure and the default cooldown is 180 seconds. While open, new Codex attempts fail quickly instead of occupying the single-concurrency queue through the legacy 300/900/1800-second retry sleeps.

Optional environment overrides:

```dotenv
CONVEYOR_PROVIDER_CIRCUIT_THRESHOLD=1
CONVEYOR_PROVIDER_CIRCUIT_SECONDS=180
```

A successful attempt resets the circuit to `healthy`. Explicitly saving provider settings also clears stale health for that provider, so replacing a key or endpoint is not blocked by the previous configuration's cooldown.

The authenticated `GET /api/config/provider` response includes the redacted health snapshot alongside the existing provider settings. No automatic provider fallback is performed; switching providers remains an explicit operator action.
