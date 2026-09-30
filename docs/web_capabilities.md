# Web Capabilities

Conveyor's web stack is intentionally split into four primitives:

```text
Search  -> discover current sources
Fetch   -> read a known URL as clean content
Browser -> deterministically control an interactive page
Agent   -> complete a goal-driven, multi-step web workflow
```

Higher-level workflows should depend on `personal_tools.web_runtime.WebRuntime`
instead of depending directly on a vendor API.

## Why this layer exists

The four primitives have different cost, latency, safety, and reliability
profiles. A task that only needs a current fact should not launch a browser,
and a deterministic browser script should not require an autonomous web agent.

The abstraction also keeps the orchestration layer independent from providers.
For example, Search can be backed by Brave, Tavily, SearXNG, Serper, or
TinyFish without changing the code that asks Conveyor to search.

## Current implementation

| Primitive | Current provider | Status |
| --- | --- | --- |
| Search | `WEB_SEARCH_BACKEND` adapter | implemented |
| Fetch | existing SSRF-hardened `web_fetch` path | implemented |
| Browser | provider slot | not configured by default |
| Agent | provider slot | not configured by default |

`WebRuntime.from_settings(settings)` preserves the existing behavior:
Search is available only when `WEB_SEARCH_BACKEND` is enabled, and Fetch is
available only when `WEB_FETCH_ENABLED=true`. Browser and Agent fail closed
until an explicit provider is wired.

Existing `/web_search` and `/web_fetch` commands remain compatible.

## TinyFish Search

TinyFish is available as a Search backend:

```bash
WEB_SEARCH_BACKEND=tinyfish
WEB_SEARCH_API_KEY=...
# optional override:
# WEB_SEARCH_ENDPOINT=https://api.search.tinyfish.ai
```

Conveyor sends the key in the `X-API-Key` header, never in a subprocess
argument, and normalizes TinyFish results into the existing `SearchResult`
shape.

TinyFish Search is only a provider for the Search primitive in this phase.
Adding TinyFish Fetch, Browser, or Agent should be done through the matching
provider slot rather than adding provider-specific branches to orchestration
code.

## Routing rule

Prefer the least-powerful primitive that can complete the task:

1. **Search** when the task needs discovery or fresh sources.
2. **Fetch** when a URL is already known and only content is needed.
3. **Browser** when code must click, type, navigate, or preserve page state.
4. **Agent** when the workflow itself requires multi-step decisions.

Human Takeover remains a separate safety boundary. Browser or Agent providers
must not bypass Conveyor's existing human-only handling for secrets, payment
details, CAPTCHA, identity verification, or consent-sensitive steps.

## Migration path

New web-oriented workflows should accept or construct a `WebRuntime`. Existing
Search and Fetch call sites can migrate incrementally; the compatibility
functions in `web_search.py` and `web_fetch.py` remain stable.

A later PR can add a provider router that chooses among local Browser,
TinyFish Browser, or other remote browser infrastructure while preserving the
same orchestration contract.
