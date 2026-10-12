# G2: typed GET interaction on operator-approved external pages

**Status: default OFF / fixture tested only / NOT production security certification.**
Requires CONVEYOR_REAL_BROWSER=1, CONVEYOR_BROWSER_EXTERNAL_READONLY=1,
and CONVEYOR_BROWSER_EXTERNAL_INTERACT=1. Ordinary chat-tool calling must
also be enabled. G2 never replaces G1 or the existing workflow fixture runner.

The model-visible browser.browse_site tool accepts a JSON string in arg:

    {"url":"https://public.example.org/approved","actions":[
      {"type":"follow_link","text":"Next"},
      {"type":"paginate"},
      {"type":"expand","index":0},
      {"type":"scroll","pixels":500},
      {"type":"search_get","query":"public records","index":0}]}

The existing authenticated Approval Inbox confirms the **exact starting URL**
for the operator, as with browser.read_webpage. Every browse call starts a
fresh isolated G1 Chromium context with its own short-lived task; no cookies
or login sessions are reused. Following links/pagination requires each target
path to have a separate, unexpired, exact URL grant issued to the **same
operator** through the existing G1 flow. Page text never grants approval.
Only GET navigation is permitted. No cross-host links, POSTs, downloads,
logins, password filling, uploads, page-provided selectors, arbitrary JS
or model-supplied destination URLs.

GET search **needs a further query-specific operator authorization**.
It is *not* automatically granted by approving the initial browse call.
After obtaining an exact URL grant for the GET search path, trusted operator
code (never an agent tool) can call:

    from personal_tools.browser_interact import approve_query_path
    import time
    approve_query_path(settings, operator_id="the-authenticated-operator",
                       url="https://public.example.org/search",
                       field="q", expires_at=time.time()+300)

This persists only the exact path, input field, operator hash and expiry;
never query values. Such approvals last at most one hour. Search requires a
visible HTML form with method=GET, exactly one type=search input with a name
and no hidden fields or extra controls (except a submit button). The agent
does not submit forms: it synthesizes the single canonical approved GET URL
and sends it through G1's pinned TLS single-hop transport. Search values are
restricted to short ASCII public-topic text. Do not search for credentials,
private identifiers or other sensitive information.

Following links is **not** a Playwright click: the engine checks visibility
and center-point hit-test, rechecks the link list against DOM swaps, then
navigates to the captured exact href via G1. Native details/summary expanding
and bounded scrolling are supported. JavaScript stays disabled, so JS-only
sites are deliberately unsupported. Iframes, closed shadows and sensitive
fields fail closed; every step emits a fixed-code redacted egress journal
event. Limits are eight actions, at most ten requests, a 25-second internal
total deadline, and an enclosing 30-second agent deadline. Every result is
nonce-wrapped UNTRUSTED_WEBPAGE data.

## Test / deployment boundary

Unit and required offline TLS Chromium integration tests cover opt-in, exact
operator grants, schema rejections, GET search restrictions, malicious page
instructions, cross-host/unapproved targets, overlays, password/iframe realms,
DOM swaps and native expansion. These do not demonstrate OS-level egress
isolation or production safety. G1's unresolved network-namespace capture,
background traffic, browser crash, takeover race, DNS rebinding and
independent review acceptance gates remain outstanding. Even GET links may
have side effects on poorly designed sites. Do not enable G2 on live
unaudited sites; G3 transaction/write behavior remains prohibited.
