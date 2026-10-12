# P0: Browser Executor Bridge — architecture and safe fixture contract

Status: **Draft / mocked execution only**. This PR is independent of, and
will need integration with, [PR #107](https://github.com/mammut001/Conveyor/pull/107).
It must not be deployed as a production browser executor.

## Why this is separate from Durable Workflows

Conveyor currently has:

- `personal_tools.web_runtime.WebRuntime`: Search / Fetch are implemented,
  Browser / Agent are **unconfigured provider slots**.
- `desktop_computer_loop.py` + `desktop_x11.py`: GUI observation/click/type
  through existing Computer Use, respecting Human Takeover leases.
- `human_takeover.py`: exclusive waiting/human-active lease per desktop scope.
- PR #107: an opt-in persistent checklist and CAS state machine, **not** an
  executor. Its `confirm_human` is ledger confirmation, not payment approval.

The missing piece is a typed, reviewable, replay-aware bridge between a
workflow's **declared action** and a browser provider's **observed effect**.

Do not simply turn a workflow label into a free-form browser instruction.

## Current implementation

This PR provides three independent Python modules:

- `workflow_browser_contract.py`: parse a small immutable, allowlisted
  `visitor600_mock` plan into typed actions and a canonical SHA-256 digest.
- `workflow_browser_bridge.py`: executes only `FixtureBrowser` (in-memory;
  **no networking, no Playwright/CDP, no X11, no browser launch**) and checks
  read-back after each operation.
- `workflow_browser_journal.py`: private SQLite two-phase action journal,
  `dispatched` -> `verified`. A `dispatched` action left by a crash is
  **uncertain** and will not be replayed automatically.

This is executable architecture, **not** an enabled Web endpoint or
`WebRuntime` provider. No configuration setting enables it against a real
browser. No production services are modified.

## Minimal deterministic fixture

The only allowed fixture ID is `visitor600_mock`, with pages:

```text
idle --open/welcome--> welcome --click/next--> details --click/next--> review
                                              |
                                              +-- fill purpose/funding
                                              +-- assert_field read-back
```

Allowed operations are `open`, `click`, `fill`, `assert_field`,
`assert_text`. Allowed fields are synthetic `purpose` and `funding`,
with short ASCII placeholder values. `click` can only target the known
`next` navigation control. There is **no submit, file upload, payment,
script evaluation, arbitrary selector, URL, login, credential or network
request capability**.

Example fixture plan (synthetic data only):

```python
from workflow_browser_bridge import FixtureBrowser, execute_fixture_plan
from workflow_browser_journal import BrowserAttemptJournal
from pathlib import Path

plan = {
    "plan_id": "mock_visitor600_001",
    "fixture": "visitor600_mock",
    "actions": [
        {"kind": "open", "on_page": "idle", "target": "welcome",
         "expect_page": "welcome"},
        {"kind": "click", "on_page": "welcome", "target": "next",
         "expect_page": "details"},
        {"kind": "fill", "on_page": "details", "target": "purpose",
         "expect_page": "details", "value": "tourism"},
        {"kind": "assert_field", "on_page": "details", "target": "purpose",
         "expect_page": "details", "value": "tourism"},
        {"kind": "click", "on_page": "details", "target": "next",
         "expect_page": "review"},
        {"kind": "assert_text", "on_page": "review", "target": "mock_review",
         "expect_page": "review"},
    ],
}
browser = FixtureBrowser()
journal = BrowserAttemptJournal(Path("/tmp/private-test-workflow-0700/attempts.sqlite3"))
result = execute_fixture_plan(plan, browser=browser, journal=journal)
assert result.status == "verified"
```

The journal contains **only** plan/action digest and index, safe enum names,
pre/post page IDs and status. No form values, raw DOM, screenshots, passwords,
headers or cookies are stored in the journal. Private journal dir is 0700,
database is 0600, and dangerous symlink/public-path use is refused.

## Execution and crash semantics

```text
validate immutable plan
  |
  v
check current observed page
  |
  v
BEGIN IMMEDIATE: reserve (plan_id, ordinal) as dispatched
  |
  v
fixture driver applies one typed action
  |
  v
read back page and field/assertion
  |
  v
BEGIN IMMEDIATE: mark verified
  |
  v
next action
```

If a process dies between dispatch and verification, the page **might** have
changed. On restart, execution stops with `reconciliation_required`, even if
the current page looks right. Never assume a click / payment / upload is
idempotent because a Python request ID is.

Verified actions are skipped on same-session replay, provided the latest
checkpoint's observable page/field still matches. If a browser context
is lost, return `page_state_drifted` and require reconciliation, **not**
automatic navigation through already-executed steps.

This is safer than retry-on-exception, but it is not yet a complete recovery
system: operators cannot mark an uncertain action reconciled through this PR,
and journal/workflow state are not yet one atomic unit.

## Production architecture to implement next (separate reviewed PR)

```text
Web Workbench: planned step, operator scope + user consent
      |
      v
PR #107 WorkflowStore: CAS revision + current step + immutable plan ID
      |
      v
Browser Executor Bridge: validate typed bounded action
      |
      +--> Policy: domain/URL egress, data classification, allowed actions,
      |            per-agent session + site ownership
      +--> Lease: verify correct HumanTakeoverStore scope and GUI exclusivity
      +--> Journal: prepare -> dispatch -> observe -> reconcile/verify
      |
      +--> BrowserPort: Playwright/CDP isolated context (preferred)
      |          or scoped X11 Computer Use fallback
      |
      v
Observed page/DOM read-back + evidence digest (no PII)
      |
      v
WorkflowStore commits checkpoint only after verified result
```

Required invariants before permitting *any* real portal action:

1. **No free-form labels as commands.** A schema-approved action plan must be
   created and reviewed separately from natural-language prompts.
2. **Explicit real-website origin policy.** Validate scheme, host, redirects,
   DNS/rebinding/SSRF, navigation/frame boundaries and downloads *at the
   transport layer*, not only at initial URL entry. This mock PR has no URLs.
3. **Sensitive input is human-owned.** Passwords, MFA, CAPTCHA, credit cards,
   final legal declarations and submission must remain behind explicit
   handoff/approval. Suspend screenshots and automated actions during takeover.
4. **Resume after handoff without skipping work.** A human interruption in the
   middle of a browser step must resume/reconcile its remaining substeps
   rather than marking the whole workflow step complete (see review on #107).
5. **Separate irreversible-action consent.** A ledger's `confirm_human` is
   not authorization for upload, payment, sending, or final submit. Require
   an independent approval transaction and verified receipt / server state.
6. **At-least-once recovery is unsafe for side effects.** Keep the persistent
   dispatch-before-act journal; uncertain outcomes require read-only
   reconciliation, never blind retry.
7. **Secure document vault before real applicants.** No PII in checklist
   labels, journal, LLM logs, screenshots, unencrypted browser artifacts or
   arbitrary worktrees; per-owner ACL, retention and delete policy required.
8. **Bound resource use.** Per-page and total timeouts, action limits, rate
   limits, stop/cancel propagation and exclusive browser context ownership.
9. **Test the original X11 policy too.** Do not duplicate or weaken app
   allowlists, `takeover_blocks_automation`, task claim fencing, or
   `WebRuntime` provider capability gating.

## Dependency and staging order

1. Merge/review PR #107 **after** its local and isolated VPS tests; first
   correct the mid-step human-resume and ambiguous frontend retry semantics.
2. Merge this PR independently (it adds only unused mock modules/tests/docs)
   after CI and architecture review. Neither PR automatically enables a live
   browser executor or should trigger VPS deployment.
3. Integrate typed plan IDs and the journal with PR #107's CAS database and
   explicit operator identity, with a mock HTTP/UI journey.
4. Introduce a *separate opt-in* Playwright/CDP fixture adapter constrained
   to a private test site and real browser read-back; retain the mock runner
   as a no-network test.
5. Add scoped X11 fallback + Human Takeover resumed cursor and a secure document
   workspace. Real ImmiAccount remains out of scope until explicit review.

## Test command

```bash
python -m unittest tests.test_workflow_browser_bridge -v
python -m unittest discover -s tests -v
make smoke
```

## Test intent

The fixture tests verify successful multistep navigation, readback, immutable
plan hash binding, all-complete replay without reissuing actions, interrupted
click ambiguity, restart from a committed prefix, page drift on lost context,
SQLite filesystem permissions, and rejection of real URLs, credential fields,
script execution, upload, payment and submit actions.

**Success means the architecture is ready for an explicitly gated fixture
Playwright adapter. It does NOT mean Conveyor can safely fill or submit a real
visa application.**
