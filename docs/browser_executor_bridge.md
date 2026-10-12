# P0: Durable Workflow x local-only Playwright Browser Executor

Status: **Opt-in local fixture only — NOT a production browser executor**.
Built on Durable WorkflowStore (#107) and the mock browser bridge (#108).
No WebRuntime provider registration, production browser automation, or automatic
deployment has been added. The original in-memory FixtureBrowser remains the
default and runs without Playwright installed.

## Capabilities and hard limits

- `workflow_browser_contract.py` continues to accept **only** the immutable
  `visitor600_mock` action plan: `open`, `click`, `fill`,
  `assert_field` and `assert_text`; no raw URLs, selectors, JavaScript,
  uploads, payments, final submit, credentials or free-form instructions.
  Synthetic fields are exactly `purpose` and `funding` with bounded ASCII
  values; the only click control is the navigation-only `next` link.
- `workflow_browser_bridge.py` provides `execute_fixture_plan` (mock-only)
  and `execute_workflow_browser_plan` (explicitly bound to a running
  WorkflowStore browser step). No automatic workflow `begin` or human approval.
- `workflow_browser_playwright.py` adds `LocalPlaywrightBrowser`, with the
  same `page`, `apply`, `readback` and `field_matches` port as the mock.
  It can be constructed **only with `CONVEYOR_REAL_BROWSER=1`**;
  the flag is also checked before each action. Importing the repo or running
  mock/unit suites requires no Playwright installation (lazy import).
- Real network traffic is restricted to `http://127.0.0.1:PORT` or
  `http://localhost:PORT` with an explicit port and *exactly* the three
  checked-in HTML files under `tests/fixtures/browser_portal/`. All requests
  are intercepted, require GET, reject redirects, and compare the response
  body SHA-256 with the corresponding bundled fixture file before allowing it
  to render. A random local HTTP service, an altered page, any other path or
  external site is rejected. Requests are not sent to external origins;
  blocked attempts poison the context. No background third-party resources,
  service workers, downloads or popups are permitted.
- Browser DOM readback validates the expected page marker and, for synthetic
  text inputs, the *actual DOM value* (also on the read-only review page).
  Only an attested link to the next local fixture page may be clicked.
  Sensitive input types, contenteditable, file inputs and submit controls
  are refused even when detected in the DOM. No real credentials or applicant
  information should be entered or stored in this fixture.

**This is real Chromium DOM interaction, but only with a bundled toy portal,
not an external browser provider or a general-purpose browser agent.**

## Enabling the isolated fixture runner

Optional development-only installation:

```bash
python -m pip install playwright
python -m playwright install chromium
```

Serve **only the bundled** `tests/fixtures/browser_portal` folder bound to
127.0.0.1 on a randomly assigned free port (the integration test does this).
Then launch a private Playwright context and use the approved local origin:

```python
import os
from pathlib import Path
from types import SimpleNamespace
from durable_workflows import WorkflowStore
from workflow_browser_bridge import execute_workflow_browser_plan
from workflow_browser_journal import BrowserAttemptJournal
from workflow_browser_playwright import LocalPlaywrightBrowser

os.environ["CONVEYOR_REAL_BROWSER"] = "1"
# origin must be the loopback host/port of the running bundled fixture server.
origin = "http://127.0.0.1:PORT"
store = WorkflowStore(SimpleNamespace(
    codex_memory_root=Path("/private/workflow-sandbox"),
    workflows_enabled=True,
))
# Operator creates a browser-only workflow and explicitly calls
# store.transition(..., action="begin", expected_revision=0, ...).
# The caller supplies that workflow id and a contract-valid visitor600_mock
# plan; see tests/test_workflow_browser_playwright.py for an executable example.
with LocalPlaywrightBrowser(origin=origin) as browser:
    result = execute_workflow_browser_plan(
        plan, browser=browser,
        journal=BrowserAttemptJournal(
            Path("/private/workflow-sandbox/browser/action-journal.sqlite3")),
        workflow_store=store, workflow_id=workflow_id,
    )
```

This example is intentionally schematic: replace `PORT`, `plan` and
`workflow_id` with local fixture test values, not live site/account data.
Use `TemporaryDirectory` for testing and keep storage private (0700 parent,
0600 SQLite DB). The regular workflow ledger feature flag is separately
required: `workflows_enabled=True` in the programmatic example; the Web
setting `CONVEYOR_WORKFLOWS_ENABLED` remains default-off.

## Journal / workflow crash recovery

```text
operator: WorkflowStore.create + begin (browser step must be running)
       |
       v
bind plan ID + digest to workflow ID and step ordinal (SQLite)
       |
       v
check WorkflowStore active, not paused/human-owned
       |
       v
journal.dispatch (plan ID, ordinal, digest): dispatched
       |
       v
allowlisted Playwright action --> actual page/field DOM readback
       |
       v
journal.verify: verified  --> next action
       |
       v
after every action verified: WorkflowStore.complete (CAS revision)
```

- Journal stores **only** enum action type, page identifiers, plan digest,
  dispatch/verification status and immutable workflow/step binding, never
  fields, URL text, screenshots, cookies, raw DOM or secrets.
- Verified journal actions form the authoritative contiguous replay prefix.
  Re-opening the journal and workflow store after a crash resumes *from the
  prefix* only when the **same active browser context** still shows the
  expected page and synthetic fields. Verified actions are not re-clicked.
  When the browser context was lost or DOM drifted, return
  `reconciliation_required`: no automatic back-navigation or restore.
- A `dispatched` but unverified action is *uncertain*. Even if it looks
  successful, do not replay or auto-mark verified. Human review is required.
  A crash after final journal verification but before WorkflowStore CAS
  completion safely reuses the verified journal prefix and advances the
  ledger when the live DOM still verifies.
- If a `request_human` workflow checkpoint has been set,
  `waiting_for_human` **blocks further automation**. `confirm_human`
  moves a running browser step to `needs_reverification` rather than
  completing it. Explicit `resume_verified` is required before any remaining
  work resumes. State is rechecked before and after every action; a concurrent
  takeover between dispatch and verification leaves an uncertain journal
  entry, never silently skips the step.
- WorkflowStore and the browser action journal remain **separate databases,
  not one atomic transaction**. This design errs on requiring operator
  reconciliation over guessing whether an external side effect happened.
  Do not use it for production browser activity.

## Tests

```bash
python -m unittest tests.test_workflow_browser_bridge -v
python -m unittest tests.test_workflow_browser_playwright -v
python -m unittest tests.test_durable_workflows -v
# Also pytest-compatible if pytest is installed:
pytest -q tests/test_workflow_browser_playwright.py
```

Without Playwright installed, all mock/workflow tests run, and the real
Chromium tests are skipped. If the Python package is installed but the
Chromium executable is missing, Chromium integration cases also skip.
The real tests start a `ThreadingHTTPServer` bound to 127.0.0.1 on an
ephemeral port, serve only the bundled static fixture, and verify live DOM
readback, journal states, crash uncertainty and network/field denial.

## Production gates that remain out of scope

This PR is **not** suitable for actual web portals, immigration applications,
credentials, MFA or payments. Before proposing any real-site adapter: separate
review for origin/DNS/SSRF and redirect defenses; explicit user/site consent
and credential vault; Human Takeover lease exclusivity; real-site evidence
and reconciliation; sandboxing and isolation; persistent browser session
ownership; deadlines/rate-limits; irreversible-action approval; independent
final submit verification; and X11/WebRuntime privilege boundaries.
