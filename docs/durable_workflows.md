# Durable Workflows (P0 foundation, draft)

A persistent, **operator-driven** checkpoint ledger for long-running real-world tasks.
The first vertical slice is **not** a browser-driving workflow agent. The product's
executor, document vault and live portal adapter are intentionally future work.

## Enable / safety

- Set `CONVEYOR_WORKFLOWS_ENABLED=true` on a test Web service; default is **false**.
- With the flag off, workflow API calls return 409 and no workflow DB is opened/created.
- With the flag on, the authenticated Web Workbench shows a **Workflows** tab on
  desktop and in the mobile **More** sheet.
- DB: `<CODEX_MEMORY_ROOT>/state/workflows.sqlite3` (new database mode 0600).
  Back it up with SQLite's online backup API when upgrading.
- Existing queued jobs, worktrees, approvals, Human Takeover and Browser
  session semantics are untouched.
- **Never store** a passport number, birth date, applicant address, bank statement,
  login, card details or other personal documents in workflow title or labels.
  String labels are stored verbatim except rejected known secret patterns.
- This release does **not** move documents into a private vault, operate a portal,
  perform a click or mark an actual external application as lodged.

## State machine

An ordered plan contains 1-40 steps, each of:

| Kind | Behaviour |
|---|---|
| `work` | Manually mark begin/complete/fail for ordinary work |
| `documents` | Progress marker only; no files opened or uploaded |
| `browser` | Progress marker only; no browser launched |
| `human` | Starts at a blocking manual checkpoint |
| `submission` | Starts at a blocking manual checkpoint; **never submits anything** |

Each workflow starts with `revision=0`, first step `ready` (or
`waiting_for_human`), and subsequent steps `pending`. Every accepted
transition increments revision **atomically** in SQLite. No step can skip an
incomplete predecessor. For non-human steps: `begin` -> `running`,
`complete` -> `completed`; `fail` -> `failed`, `retry` -> `ready`
(up to 5 attempts). During `running`, `request_human` changes the step
to `waiting_for_human`. Only `confirm_human` can advance that checkpoint.
Pause/resume and cancel are explicit; completed and cancelled workflows
are terminal. The state engine does not invoke external tools.

**Human checkpoint confirmation is not payment authorization, a visa
lodgement, an MFA bypass or proof that any outside system changed.** It records
only that the operator says a checkpoint was handled. Future executor
integration must have a *separate* capability gate and external verification.

## Concurrency, crashes, and retries

- All writes use `BEGIN IMMEDIATE` and compare the provided
  `expected_revision` against the current revision.
- Each create and action requires a stable `request_id` (8-96 URL-safe chars).
  Create idempotency compares the canonical plan hash. Action idempotency
  compares action + expected revision and returns the **original** result with
  `replayed: true`.
- A stale revision or reused request ID with a different action gives HTTP
  **409**, not a second transition.
- On restart, plans, step statuses, attempt counts and accepted events remain
  in SQLite; a `running` checkpoint remains **running** until explicitly
  resolved. No automatic replay or duplicate external side effect is possible
  in this release because no external action executor exists.
- An idempotent replay returns the *original action snapshot*; fetch GET to
  see the latest workflow state after concurrent changes.

## Authenticated Web endpoints

Every route except existing `/api/health` requires the normal Web Bearer token.
Feature disabled: 409. Not found: 404. Invalid shape: 400. Stale revision,
invalid transition or idempotency collision: 409.

```http
GET /api/workflows
GET /api/workflows/<workflow_id>
POST /api/workflows
Content-Type: application/json
Authorization: Bearer <web-console-token>

{
  "title": "Australia Visitor 600 mock",
  "request_id": "create-demo-001",
  "steps": [
    {"kind": "documents", "label": "Review generic document checklist"},
    {"kind": "browser", "label": "Simulate form draft on a local fixture"},
    {"kind": "human", "label": "Human verifies identity and MFA"},
    {"kind": "work", "label": "Validate simulated fields"},
    {"kind": "submission", "label": "Operator-only final review checkpoint"}
  ]
}
```

Advance only the current step:

```http
POST /api/workflows/<workflow_id>/action
Content-Type: application/json
Authorization: Bearer <web-console-token>

{"action": "begin", "expected_revision": 0, "request_id": "action-demo-001"}
```

Actions: `begin`, `complete`, `fail`, `retry`,
`request_human`, `confirm_human`, `pause`, `resume`, `cancel`.
`confirm_human` requires the step to be `waiting_for_human`. The Web UI
asks for an explicit confirmation before marking a human checkpoint done.

## Australia Visitor 600 example (mock, not live)

The operator-supplied Tourist-stream skill describes: choosing the correct
stream, identifying travel/funding/accommodation details, preparing supporting
evidence and any required Form 54, completing ImmiAccount pages, handing MFA
to a human, auditing the draft, human-only payment/lodgement, then monitoring
Messages/Biometrics/Health and the grant letter.

The built-in demo uses **only generic labels** and a fake portal. It never
touches ImmiAccount, does not include applicant information or produce a
government application, and deliberately ends at a human-only final checkpoint.
The official checklist, current fees, stream eligibility and form requirements
must be verified separately before any future production adapter is written.

## Roadmap: integration gates, not claims of existing capability

1. **Executor bridge:** typed step contracts; scoped Web Agent/browser provider
   from `WebRuntime` or existing desktop Computer Use with observation and
   DOM/read-back validation. Never translate a ledger `begin` to a real
   click until explicitly authorized. No arbitrary URLs/shell from labels.
2. **Evidence and file vault:** private encrypted-at-rest storage for PII,
   access-controlled document references, retention/deletion, translation
   provenance, PDF validation, secure uploads; *never* use the ledger for PII.
3. **Secure Human Takeover wiring:** pause automation and screenshot while the
   approved user enters MFA, passwords or payment. Assert cleanup before
   resume; capture no secrets.
4. **External side-effect journal:** prepare/confirm/commit per irreversible
   action, portal receipt verification, replay-safe reconciliation and explicit
   final user consent; assume external actions are non-idempotent.
5. **Long-running monitoring:** opt-in scheduled status checks with expiry,
   consent, secure session handling and actionable notifications.

## Tests

```bash
python -m unittest tests.test_durable_workflows -v
python -m unittest tests.test_workflow_web_api -v
python -m unittest discover -s tests -v
make smoke
cd web && npm ci && npm run typecheck && npm run lint && npm run build
```

The HTTP integration suite starts a loopback test Web server with a fake
operator and no production credentials. It tests authorization, flag-off
behaviour, persistence and 409 conflicts. The unit suite tests concurrent
CAS, duplicate retry, reboot restoration, pause/retry/cancel and the
`human` / `submission` fail-closed boundaries.

Do not enable this experimental feature on the production VPS until CI
passes and the operator explicitly approves a separate deployment.
