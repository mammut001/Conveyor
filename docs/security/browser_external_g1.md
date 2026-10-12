# External browser G1 implementation notes (not production certified)

An independently constructed, read-only, unauthenticated Chromium session is available through workflow_browser_external.ExternalReadOnlyBrowser. It is deliberately NOT registered as a workflow action executor, Web endpoint, or replacement for the existing fixture runner. The returned UntrustedPageText is always untrusted data, never instructions or approval.

## Operator approvals and defaults

Both CONVEYOR_REAL_BROWSER=1 and CONVEYOR_BROWSER_EXTERNAL_READONLY=1 are required; both default OFF. Out-of-band trusted operator code must call ApprovalLedger.approve(task, host, port, expires_at, approver, safe_paths, methods). This records a task-specific exact lowercase ASCII HTTPS hostname/port, explicitly reviewed GET-safe paths, optional HEAD methods, approver, and at most one-hour expiry in SQLite. An approval UI is NOT part of this branch. No webpage text or agent workflow can create approvals through the external browsing API.

Example trusted operator setup (not executed by the agent):

```python
import time
from workflow_browser_external_approvals import ApprovalLedger
db = '/home/operator/.conveyor/browser-external.sqlite3'
ledger = ApprovalLedger(db)
ledger.approve(task='review_123', host='public.example.org',
               expires_at=time.time() + 300, approver='operator_1',
               safe_paths=('/public-info',), methods=('GET',))
```

Example bounded read-only caller:

```python
import asyncio
from workflow_browser_egress import EgressAudit
from workflow_browser_external import ExternalReadOnlyBrowser
async def main():
    async with ExternalReadOnlyBrowser(task='review_123', ledger=ledger,
                                       audit=EgressAudit(db)) as browser:
        page = await browser.read('https://public.example.org/public-info')
        assert page.trust_level == 'untrusted_page_data'
        return page
asyncio.run(main())
```

ApprovalLedger.revoke(task) revokes immediately. CONVEYOR_BROWSER_EXTERNAL_KILL_FILE is a file-existence kill switch, default /tmp/conveyor-browser-external.kill; creating the file closes an active session and forbids new navigation. Turning off either environment flag in the running process also fails closed. Reuse is prevented: one read and one newly launched browser/context per task. No shared profile or stored credentials.

SQLite parent directories must be private (0700); DB files are 0600. The redacted egress journal includes only timestamp, task, approved domain, HTTP method, outcome and fixed denial code. No URL paths, query values, cookie/token values, payloads or page content are stored. An unavailable audit prevents any outbound request.

All request paths and methods must be explicitly reviewed as read-only. JavaScript, service workers, subresources, popups, WebSockets, download acceptance, and author-provided cookies are blocked. Chromium resolves the hostname to an IP once before launch, rejects non-global addresses, and pins the chosen public IPv4 using host-resolver-rules. HTTPS certificates remain validated; redirects and navigation away from the approved URL are denied.

## Offline tests and outstanding release gates

The required Chromium CI job runs tests/test_workflow_browser_external.py against a local HTTPS server with a self-signed certificate and Chromium hostname mapping. Only in those isolated tests are loopback address pinning and self-signed certificate acceptance injected; real browsing still rejects both.

IMPORTANT: This is NOT final G1 approval. Chromium request interception and DNS pinning are not an OS/network-namespace firewall. Independent review, packet-capture verification of all browser traffic, hostile CDN/iframe/GET side-effect tests, cross-task process isolation, crash cleanup, expiry/kill races, and a genuine operator approval UI remain required before enabling external sites. Keep default flags OFF and leave the external runner disconnected from automated workflow dispatch.
