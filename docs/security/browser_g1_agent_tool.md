# G1 read_webpage agent tool (default OFF)

The personal assistant now exposes `browser.read_webpage` in its existing
OpenAI-compatible tool registry **only when** both
`CONVEYOR_REAL_BROWSER=1` and
`CONVEYOR_BROWSER_EXTERNAL_READONLY=1` are set. Regular chat tool-calling
must also be enabled separately. This tool is not the legacy `web.fetch`
(curl) tool and does not change that tool's configuration or security model.

## Approval lifecycle

1. The agent requests one exact, canonical HTTPS URL with no query, fragment,
   credentials or localhost suffix. The request is **not** a network fetch.
2. Without a valid same-operator grant for that exact URL, the tool creates a
   normal pending confirmation using the existing `_request_confirmation`,
   `PendingToolAction`, Web Approval Inbox and optional Telegram/Feishu relay.
   The chat loop returns `awaiting approval`. The pending confirmation uses
   the existing short confirmation expiry (5 minutes).
3. Rejecting the pending confirmation never creates a browser grant. Approving
   through the existing authenticated confirmation route writes a scoped
   **24-hour** grant in the private `CODEX_MEMORY_ROOT/browser_external`
   SQLite database, then retries the exact URL read and delivers the result to
   the origin conversation. Approvals are bound to operator identity and URL;
   they do not authorize sibling paths, subdomains, HTTP, arbitrary ports or
   other operator IDs.
4. Subsequent reads of the same URL while the grant is valid do not need an
   additional prompt. Every read mints a unique one-use *underlying G1 task*
   with an expiry no longer than one hour or the remaining grant lifetime.
   This preserves the short-duration security invariant of the G1
   `ApprovalLedger`. It is not a 24-hour reusable browser profile.
5. Disabling either browser flag, invoking the kill switch, losing the audit
   store, or expiring the scoped grant must fail closed; the browser's
   DNS/IP/TLS/redirect, method/path, subresource, cookie and download checks
   remain intact. No browser sessions are shared across reads.

The tool returns at most 3,000 characters of page text inside explicit
`UNTRUSTED_WEBPAGE_BEGIN`/`UNTRUSTED_WEBPAGE_END` markers. The chat tool
loop adds its existing untrusted `tool-result` wrapper, escaping closing
tool tags. Page content is never instructions, never a source of approvals,
and must not be used to trigger privileged follow-up actions.

## Tests and limitations

`tests/test_browser_agent_read.py` mocks Chromium and tests feature flags,
pending inbox records, approve/retry, rejection, scope isolation, expiry
and untrusted result marking. `tests/test_workflow_browser_transport.py`
tests the 20-second single-hop transport deadline, Content-Length framing,
body size and non-identity Content-Encoding without Chromium. The separate
real-browser Chromium fixture job remains mandatory for G1 regression tests.

**G1 is not a production security certification.** See
[browser_external_g1.md](browser_external_g1.md) for unresolved external-site
hardening, independent review, packet-capture and default-off requirements.
A human should review the exact GET path before approving: even GET may have
server-side effects on poorly designed websites. Avoid pages requiring login,
credentials or private data. The tool does not support search, POST, clicks,
forms, uploads, downloads or arbitrary site browsing.
