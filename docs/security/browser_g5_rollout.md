# G5 — Controlled rollout and continuous regression (default OFF)

**Status: experimental rollout control ONLY, NOT production authorization.**
G1–G4 lack independently verified process-wide Chromium egress isolation,
hostile-process resistance, and complete human-only final-submit contracts.
Green tests do not resolve these threats. In particular, payments, purchases,
2FA and final account changes remain **human-only**. G5 never grants a browser
tool, creates a domain approval, or promotes a site based on CI status.

## Flags / deployment

- `CONVEYOR_BROWSER_G5=1`: enable the G5 status/metrics/emergency-revoke API
  and per-session sticky revoke checks; **disable all G3 server-dispatched final
  submit paths**, even in monitoring mode when `CONVEYOR_BROWSER_G5_LIVE_ACK`
  is unset. Default off; no changes to preexisting routes, `/tools`, or
  G1–G4 when off.
- `CONVEYOR_BROWSER_G5_LIVE_ACK=1`: **separate manual release authorization**
  switch. If set with G5, each G1/G2/G4 active request is denied unless
  independently signed evidence permits that exact HTTPS origin and action.
  This switch is intentionally **NOT** set by CI's all-flags step, and its
  presence is never proof of OS network sandbox certification.
- `CONVEYOR_BROWSER_G5_MANIFEST=/secure/path/manifest.json`: a single reviewed
  manifest, owner-owned regular file with 0600 or stronger permissions.
- `CONVEYOR_BROWSER_G5_REVIEW_KEY`: secret of at least 32 characters held by
  an independent reviewer; keyed SHA-256 signature over canonical JSON.
  Never put this key in the repository or CI flags. Key custody separation
  is an operational requirement and cannot be established by a self-signed
  manifest or this software alone.

Example signed payload (all evidence values are **actual SHA-256 artifact
digests** from an out-of-band review, NOT the placeholder below):

```json
{
  "version": 1,
  "origin": "https://reviewed.example.org",
  "actions": ["read", "interact"],
  "reviewer": "independent-security-reviewer",
  "expires_at": 1790000000,
  "builds": {"playwright": "<installed package version>",
             "chromium": "<Playwright browsers.json chromium revision>"},
  "evidence": {
    "g1": "<sha256>", "g2": "<sha256>", "g3": "<sha256>",
    "g4": "<sha256>", "os_capture": "<sha256>", "dns": "<sha256>",
    "injection": "<sha256>", "dom_race": "<sha256>",
    "session_bleed": "<sha256>", "takeover_chaos": "<sha256>",
    "egress_alerts": "<sha256>", "rollback": "<sha256>",
    "redaction": "<sha256>"
  },
  "retention_days": 14,
  "signature": "<HMAC_SHA256(review_key, canonical JSON without signature)>"
}
```

Signer procedure: `json.dumps(payload_without_signature,
sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()`, then
`hmac.new(review_key.encode(), payload, hashlib.sha256).hexdigest()`.
The independent reviewer must verify the *content* of each referenced
artifact, including OS/socket-capture zero unauthorized connections and
third-party site consent. Software verifies signatures, schema, action,
expiry (at most 30 days), retention (1–30 days), installed Playwright version
and packaged Chromium revision; it **cannot** verify captures merely from
their SHA-256 hashes. A declared capture without independently checked
evidence is not approval to deploy.

Action vocabulary is only `read`, `interact`, `auth`. Any G5 mode,
including monitoring-only without LIVE_ACK, disables the current G3
server-dispatched submit prototype entirely. No
payments, purchases, MFA/OTP automation, or irrevocable action is certifiable.

## Authenticated operator control (Web Console)

When G5 is on:

- `GET /api/browser/g5/status`: monitoring mode, live-request state, and
  sticky revoked exact origins. `auto_promotion` always false.
- `GET /api/browser/g5/metrics`: aggregate count/reason metrics from the
  already redacted G1/G2 egress audit (last 200 records), dashboard `alert`
  at five denials or immediately for severe failure reasons. No query strings,
  tokens, form data or page bodies.
- `POST /api/browser/g5/revoke` with
  `{"origin":"https://reviewed.example.org"}`: persists a fail-closed kill
  **before** attempting bounded (8-second) G4 browser-session teardown.
  Active G1/G2 watchers observe the kill and close. A timeout returns 202
  with `pending_cleanup=true`; all future requests remain denied. The kill
  is sticky; there is **no** public un-revoke or auto-approve endpoint.
  Recovery requires new independent review and deliberate offline action.

With the flag **off**, these endpoints return the existing 404, no browser
tools are added, and no G5 state file is opened. The global browser kill switch
still exists independently.

## Acceptance / residual blockers

`tests/test_browser_g5_rollout.py` is in unit and required-Chromium CI,
including disabled-flag behavior, strict origins, independent signature,
failed/tampered/expired manifests, pinned builds, sticky revoke, G1 live
revocation and denial-alert data minimization. Previous G1–G4 Chromium tests
remain mandatory, with `--timeout=20`; every newly added browser/network
await is bounded.

Before ANY real external rollout the operator must separately demonstrate:
real OS-level zero-unauthorized-socket capture across DNS rebinding, workers,
iframes, script, WebSocket and WebRTC; adversarial prompt injection; dynamic DOM
races; session bleed; GUI/takeover chaos; end-to-end alert delivery; pinned
Chromium actual binary validation; teardown/rollback; and independent
signature/key-custody review. This PR adds **gating infrastructure**, not
a claim that these remaining G1–G4 acceptance criteria are satisfied.
