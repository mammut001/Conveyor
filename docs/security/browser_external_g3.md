# G3 — human-confirmed, non-sensitive form submission (fixture gate only)

**Status: OFF by default; NOT an authorization to enable on external production sites.**
G3 is experimental and inherits the outstanding G1/G2 OS-level egress,
takeover/ownership and independent review gaps. The original
[browser_external_review.md](browser_external_review.md) describes a stricter
G3 with human-performed final actions; this implementation is a *narrow
human-confirmed, server-dispatched prototype*, **not** fulfillment of every
original security-review gate. In particular it is not approved for financial,
credentialed, account-changing or irreversible operations.

## Flags and tool API

All four must be explicitly set to `1`:

- `CONVEYOR_REAL_BROWSER`
- `CONVEYOR_BROWSER_EXTERNAL_READONLY`
- `CONVEYOR_BROWSER_EXTERNAL_INTERACT`
- `CONVEYOR_BROWSER_EXTERNAL_SUBMIT` (new, default OFF)

Existing operator-approved, unexpired **exact G1 source AND form-action URL**
grants are required. The target must be on the same canonical HTTPS host and
port and approved path. No ambient cookies, authenticated sessions, JS, subresource
loads, redirects, file upload/download, or popup access is allowed.

The only agent-exposed API is `browser.prepare_submit` with JSON string
`{"url":"https://host.example/public/form","form_ref":0,"fields":{"topic":"hello","agree":true}}`.
`form_ref` is the visible HTML form's zero-based index (not a selector).
Allowed editable controls: ordinary visible text/search/textarea, single select
and checkbox. Named hidden and prefilled values are *included in the exact
reviewed outgoing values* but never editable by the agent. Duplicate names,
special encodings, named submit buttons, injected associated controls, hidden
editor controls, credential/OTP/credit-card/file fields, complex realms, and
suspicious checkout/login/delete URLs and page text are refused.

The preview stores, in a private 0600 SQLite DB, the source and target HTTPS
URLs, GET/POST method, every outgoing name/value pair, control and form
attributes, a PNG screenshot and SHA-256 over the canonical preview including
the screenshot digest. It returns **only the hash and awaiting-approval status**
to the agent. HTML/text from external pages is untrusted and cannot grant any
approval or invoke a dispatch operation.

## Human-only approval and dispatch

`prepare_submit` creates a fresh approval in the existing authenticated
Approval Inbox and optional Telegram/Feishu relay; expiry is **five minutes**.
The Web inbox displays all outgoing values, method, target, preview hash and
screenshot. Relay messages include the method, exact target, values and hash,
and direct operators to the authenticated Web inbox to see the screenshot.
The approval token contains only the opaque preview ID and expected hash,
not the form values. The internal `browser.g3_dispatch` is *not* a registered
agent tool. Approval requires an operator decision through the authenticated
inbox or existing channel confirmation/relay flow.

A confirmed action atomically claims its pending DB record and creates a
**new, isolated, empty** Chromium context, loads the approved source again,
reapplies the same requested non-sensitive edits, and compares the complete
new snapshot (including previously hidden fields, action URL and screenshot)
against the originally approved hash. Mismatch, expiry, revoked G1 grant,
disabled flag and kill switch all refuse dispatch. The normal G1 Chromium
request filter stays GET-only; the final one-hop GET/POST uses a **separate,
trusted, pinned-IP TLS** operation to the previously approved exact path. There
is no native browser click, page script or redirect following.

The SQLite record transitions `pending → verifying → dispatched → done`,
with `dispatched` committed **before** any outbound submit bytes are sent.
Only one claimant wins. Any failure after dispatch yields
`needs_reconciliation` and may have committed remotely. There is **no retry**
or automatic replay, including after process crash. The operator must check the
remote site independently before any new submission attempt. G3 egress logs
record fixed metadata only; they do not contain field values or response bodies.

## Deliberate constraints

This is a deliberately conservative subset: no authentication, payments,
checkout, order placement, account deletion, financial fields, JS-only forms,
complex form encoding, multiple selection, named submit controls, non-HTTPS
or cross-host actions. Dynamic screenshots or changed hidden fields fail
closed, so some harmless sites will require a fresh preview. Browser/network
policy remains Chromium interception + pinned HTTPS, **not** a proven
process-level network sandbox or coordinated GUI takeover lease. Do not treat
green fixture tests as production safety certification.

## Required regression

`tests/test_workflow_browser_g3.py` is in the required Local fixture Chromium
CI job. It exercises disabled flags, schema denial, no submit before human
approval, full preview including hidden values, changed hidden field or action,
expiration, single-use and duplicate approval, simulated uncertain outcome,
sensitive/checkout denial and untrusted page instructions. Independent G1–G5
review and packet-level adversarial validation remain mandatory before rollout.
