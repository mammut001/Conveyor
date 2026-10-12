# Browser approvals and GET search — Web Console

**Default OFF:** G2 still requires CONVEYOR_REAL_BROWSER=1,
CONVEYOR_BROWSER_EXTERNAL_READONLY=1, and
CONVEYOR_BROWSER_EXTERNAL_INTERACT=1. These flags are not toggled by this UI.

## Operator workflow

1. In the chat tool, browser.browse_site now takes a **JSON object string**
   with url and actions. Example:
   {"url":"https://public.example.org/start","actions":[
     {"type":"follow_link","text":"Next"},
     {"type":"search_get","path":"/search","field":"q","query":"public records"}]}
2. All navigation remains exact approved-host/path GET-only. For GET search,
   the declared path and field are *expected identity* — the actual visible
   GET form must have exactly the same action URL and field. Mismatches are
   refused without outbound query requests. The agent cannot provide an
   arbitrary selector, script or destination URL.
3. If the start URL or action type is not approved, the ordinary Approval
   Inbox requests an authenticated operator decision. A new action type
   requires a fresh card even if the URL had a prior approval.
4. If a search path or query field is not approved, the same card spells out
   the exact host/path and search field. Approving it records the scope, an
   exact GET path approval and the query-field grant. The query *value* is
   not saved to the approval database. The query-field grant is valid up
   to **one hour**, while the start URL + approved action-kind set is valid
   up to **24 hours**. Rejecting never creates a grant.
5. Use **Browser sites** in the Web Console to inspect active path, action
   and query permissions, operator fingerprints, scope and expiration, and
   to revoke individual entries. Approver is a pseudonymous fingerprint
   because existing G1 grants store a SHA-256 operator key rather than a
   cleartext username; no identity is invented.
6. The same view shows the latest 20–200 redacted egress records (time,
   hostname, method, allowed/denied and fixed reason). It never returns
   raw paths, query values, cookie headers, bodies or page content.
7. The global kill-switch button creates/removes the *same* private kill
   file honored by active G1/G2 contexts. The UI cannot change a custom
   kill-file location outside the application's private directory.

## Authentication and safety

GET /api/browser/approvals, GET /api/browser/egress?limit=50,
DELETE /api/browser/approvals/{hashed-grant-id}, and
POST /api/browser/kill {"enabled":true|false} reuse Web Console bearer-token
authentication and no-store JSON responses. None exposes an arbitrary grant
creation endpoint; the authenticated approval inbox remains the sole path
from a model request to operator approval. Per-step G2 checks fail closed
on revoked approvals. G1's separate short-lived sessions remain subject
to the kill switch.

G2 link discovery now rejects opacity below 0.1, dimensions below 4px,
clipped/hidden ancestors and overlays using five hit-test points. Static
trusted inspection code remains separate from model/page-authored scripts.

### Limitations

**CI is not production certification.** Real internet release still needs
independent egress packet capture, browser-process isolation, hostile-site
testing and G1/G2 security sign-off. HTTP GET can still have unintended
server-side effects on a poorly designed site. G3 writes, logins,
payments, POST forms and file uploads remain agent-disabled.
