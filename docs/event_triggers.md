# Event-triggered routines

This default-off feature extends the existing routine webhook receiver, SQLite
database, untrusted event wrapper and approval flow. Enable
`CONVEYOR_ROUTINES_ENABLED=true` and `CONVEYOR_HOOKS_ENABLED=1`.
The old `CONVEYOR_WEBHOOKS_ENABLED` path is retained for existing users.

## Authentication modes are fixed when hooks are created

Create one hook per routine through the bearer-authenticated Web Console API:

- `POST /api/hooks`: `{"routine_id":1,"mode":"generic","filter":{}}`
- GitHub example: `{"routine_id":1,"mode":"github","filter":{"event":"pull_request","actions":["opened"]}}`
- `GET /api/hooks`: list ID, associated routine, `mode`, status and filters; never the secret.
- `POST /api/hooks/<hook_id>/rotate`: new hook ID/secret; **preserves mode and filters**.
- `POST /api/hooks/<hook_id>/pause` / `resume`: pause or resume the hook.
- `DELETE /api/hooks/<hook_id>`: delete the hook.

Mode defaults to `generic`, including hooks migrated from the earlier
policy table. It cannot be chosen by inbound headers, and mixing GitHub and
Conveyor authentication headers is rejected with **401** regardless of HMAC
validity. To change modes, create a new hook with the desired mode (this
rotates the per-routine hook ID and secret). Filters can use `event`,
`actions` (exact names), and `branch` (exact `refs/heads/main`,
push events only).

Conveyor generates a distinct random 32-byte URL-safe secret for each hook.
**The HMAC secret is stored in plaintext in the existing 0600 SQLite
`routines.db` file** because verification requires the key. It is
not hashed. Secrets are returned only by authenticated creation/rotation
responses and never in lists, audit logs or webhook responses.

Chat and Telegram with chat tools enabled offer `hook.list` (READ)
and `hook.manage` (WRITE, requiring the normal human approval).
Usage: `add <routine_id> [github] [| JSON filter]`,
`rotate <hook_id>`, `pause <hook_id>`, `resume <hook_id>`,
`delete <hook_id>`. Chat replies **never contain secret values**;
after a chat-created or chat-rotated hook, retrieve the credential through
`GET /api/hooks/<hook_id>/secret` with the Web Console bearer token.
That endpoint reveals the secret **exactly once**, then returns 404. It does
not reveal secrets created through normal API responses (which already
displayed the credential once). Never enter credentials into a model prompt.

## Generic mode: timestamp + delivery-bound signature

Send `POST /hooks/<hook_id>` with `Content-Type: application/json`,
`X-Conveyor-Timestamp` (Unix seconds within 300 seconds of server),
`X-Conveyor-Delivery` (unique 1-200 character ID, ASCII letters,
digits, `._:-`), optional `X-Conveyor-Event`, and
`X-Conveyor-Signature` with the exact form `sha256=<hex>`.

The HMAC-SHA256 **message bytes** are precisely:

    f'{timestamp}.{delivery_id}.'.encode('ascii') + raw_body

The timestamp and delivery ID must be the exact strings sent in the
headers. Only the raw JSON *bytes* follow the prefix; do not parse or
re-serialize them when signing. A copied body/signature cannot be re-used
with a new timestamp or nonce.

Complete Python + curl signing example (set `SECRET` and
`HOOK_ID` using the one-time Web Console response):

    BODY='{"action":"opened","number":42}'
    TS=$(date +%s)
    DELIVERY=$(python3 -c 'import uuid; print(uuid.uuid4())')
    SIG=$(BODY="$BODY" TS="$TS" DELIVERY="$DELIVERY" SECRET="$SECRET" python3 - <<'PY'
    import hashlib, hmac, os
    body = os.environ["BODY"].encode("utf-8")
    prefix = f'{os.environ["TS"]}.{os.environ["DELIVERY"]}.'.encode("ascii")
    print(hmac.new(os.environ["SECRET"].encode("utf-8"),
                   prefix + body, hashlib.sha256).hexdigest())
    PY
    )
    curl -X POST "https://example.org/hooks/$HOOK_ID" \
      -H 'Content-Type: application/json' \
      -H 'X-Conveyor-Event: issues' \
      -H "X-Conveyor-Timestamp: $TS" \
      -H "X-Conveyor-Delivery: $DELIVERY" \
      -H "X-Conveyor-Signature: sha256=$SIG" \
      --data-binary "$BODY"

## GitHub mode: native webhook compatible

Set `mode: "github"` when creating the hook, then configure
GitHub's repository Webhook with JSON content type, the hook URL, and its
generated secret. GitHub sends `X-Hub-Signature-256: sha256=<hex>`
(HMAC-SHA256 over the **raw body only**), `X-GitHub-Delivery` (required
unique ID) and `X-GitHub-Event`. **No Conveyor timestamp is
required or accepted in GitHub mode**. Real GitHub webhook requests work
directly with no relay. Duplicated delivery IDs for the same hook are
rejected with 409. Example filters:

- `{"event":"issues","actions":["opened"]}`
- `{"event":"pull_request","actions":["opened"]}`
- `{"event":"push","branch":"refs/heads/main"}`

## Safety

- JSON objects only, maximum 256 KiB. Bounded bodies are drained fully
  before 400/401 responses; oversized bodies close without unbounded reading.
- Accepted delivery IDs are reserved transactionally in SQLite for seven
  days; duplicate is 409, including during a busy routine.
- Replay and per-hook/global rolling-minute quotas (30 / 120) are checked
  **before** routine-busy rejection. A busy 429 does not consume a nonce.
- Accepted events invoke the **existing** routine runner and approval inbox.
  Event payloads are redacted, truncated to 4,000 characters, and wrapped as
  untrusted. WRITE tools cannot bypass human confirmation.
- The endpoint returns 404 for disabled/unknown hooks, 401 for bad signatures
  or wrong mode (plus missing/stale generic timestamps), 400 for malformed
  JSON, 415 for non-JSON content type, 409 for paused/replayed, 413 for
  oversized bodies, 429 for throttling and 202 for accepted/filtered events.
- Audit lines contain only hook ID prefix, status and sanitized event.
  Never expose the bearer-authenticated `/api` routes publicly.
  Expose only `/hooks/` behind an HTTPS reverse proxy.
