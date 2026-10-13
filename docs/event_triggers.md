# Event-triggered routines

This feature extends the existing routine webhook receiver, database, and approval flow.
It is disabled by default.

Enable CONVEYOR_ROUTINES_ENABLED=true and CONVEYOR_HOOKS_ENABLED=1. The
legacy CONVEYOR_WEBHOOKS_ENABLED remains supported for backward compatibility.

## Bearer-authorized Web Console management

- GET /api/hooks: list routine associations, IDs, status and filters (no secrets).
- POST /api/hooks: create or rotate a per-routine hook. Example JSON:
  {"routine_id":1,"filter":{"event":"pull_request","actions":["opened"]}}
- POST /api/hooks/<hook_id>/rotate: invalidate old ID/secret and issue new ones.
- POST /api/hooks/<hook_id>/pause or /resume: disable or enable delivery.
- DELETE /api/hooks/<hook_id>: delete credentials.
- Existing /api/routines/<routine_id>/hook management is retained.

Only one hook exists per routine, following the existing SQLite schema. The
random secret is returned once on create/rotate and then persisted in the
existing routines.db (chmod 0600), which the receiver needs for HMAC verification.
Secrets never appear in lists. Store creation responses securely.

Chat and Telegram can access hook.list (READ) and hook.manage (WRITE, with
human confirmation). Usage: add <routine_id> [| JSON filter], rotate <hook_id>,
pause <hook_id>, resume <hook_id>, delete <hook_id>. A one-time secret shown
in chat is sensitive and may persist in its transcript.

Filters accept event, actions (exact list), and branch (push only, full
refs/heads/main style). Example GitHub issues opened:
{"event":"issues","actions":["opened"]}. Push main:
{"event":"push","branch":"refs/heads/main"}. An empty filter allows all.

## Signed POST /hooks/<hook_id>

Accepts only Content-Type: application/json, maximum 256 KiB, and an object
JSON body. Required headers are:

- X-Conveyor-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>
- X-Conveyor-Timestamp: integer Unix seconds within 5 minutes
- X-Conveyor-Delivery: unique nonce (1-200 ASCII letters/digits/._:-)
- X-Conveyor-Event: optional event name (default webhook)

GitHub-style signatures and events use X-Hub-Signature-256,
X-GitHub-Delivery and X-GitHub-Event instead. The timestamp header is still
mandatory. Native GitHub webhook deliveries do NOT contain this header.
A trusted forwarding relay must add X-Conveyor-Timestamp for a fresh delivery
without changing the signed raw body, or re-sign as a Conveyor event. Direct,
unmodified GitHub deliveries are rejected.

Example:

    BODY='{"action":"opened","number":42}'
    TS=$(date +%s)
    NONCE=$(python3 -c 'import uuid; print(uuid.uuid4())')
    SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')
    curl -X POST "https://example.org/hooks/$HOOK_ID" \
      -H "Content-Type: application/json" \
      -H "X-Conveyor-Event: issues" \
      -H "X-Conveyor-Timestamp: $TS" \
      -H "X-Conveyor-Delivery: $NONCE" \
      -H "X-Conveyor-Signature: sha256=$SIG" --data-binary "$BODY"

Bounded bodies are fully consumed before 400/401 responses; requests above
the size cap are closed after 413 rather than drained without limit. Replay
IDs are durably reserved atomically in SQLite for seven days (409 on repeat).
Accepted events are limited to 30 per hook and 120 globally per rolling minute,
and at most one in-flight run per routine (429 and Retry-After on busy).

The routine executes via the original run_single_routine webhook mode,
not a privileged new pathway. Payload is redacted, truncated to 4,000 chars
and wrapped as UNTRUSTED event data. No long-term memory is injected, and
all WRITE actions retain their existing human approval requirements.

HTTP codes: disabled/unknown 404; bad HMAC/timestamp 401; stale/replayed 409;
paused 409; malformed JSON 400; invalid content type 415; body too large 413;
rate limit 429; accepted/filtered 202. Audit records only hook-ID prefix,
status and sanitized event, never body or secret.

Expose the public /hooks/ route behind HTTPS, keeping /api management routes
private and bearer-token protected.
