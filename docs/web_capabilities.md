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
| Fetch | local safe fetch, or TinyFish when Search uses TinyFish | implemented |
| Browser | provider slot | not configured by default |
| Agent | provider slot | not configured by default |

`WebRuntime.from_settings(settings)` keeps existing defaults:

- Search is available only when `WEB_SEARCH_BACKEND` is enabled.
- Fetch is available only when `WEB_FETCH_ENABLED=true`.
- non-TinyFish search backends use Conveyor's existing SSRF-hardened local
  Fetch implementation.
- `WEB_SEARCH_BACKEND=tinyfish` pairs TinyFish Search with TinyFish Fetch using
  the same API key.
- Browser and Agent fail closed until an explicit provider is wired.

The legacy `/web_search` and `/web_fetch` command surfaces remain compatible.
Higher-level workflows should use `WebRuntime`; `research.py` is the first
production consumer migrated to the abstraction.

## TinyFish Search + Fetch

TinyFish is available through the existing search configuration:

```bash
WEB_SEARCH_BACKEND=tinyfish
WEB_SEARCH_API_KEY=...
# optional Search endpoint override:
# WEB_SEARCH_ENDPOINT=https://api.search.tinyfish.ai
```

Search sends the key in the `X-API-Key` header and normalizes results into the
existing `SearchResult` shape.

When the runtime sees `WEB_SEARCH_BACKEND=tinyfish`, its Fetch primitive uses
TinyFish's `POST https://api.fetch.tinyfish.ai` endpoint with:

```json
{"urls": ["https://example.com/page"]}
```

The clean text returned by TinyFish is normalized into Conveyor's existing
`ToolResult` shape. Before calling the provider, Conveyor still validates the
target URL using its existing public-web/SSRF policy. The TinyFish endpoint is
also validated, response size is capped by `WEB_FETCH_MAX_BYTES`, and output is
redacted/truncated through the existing safety helpers.

This pairing is deliberate because TinyFish currently uses one API key across
Search and Fetch. Conveyor never sends Brave/Tavily/Serper credentials to the
TinyFish Fetch endpoint.

## Research routing

`personal_tools/research.py` no longer calls concrete Search and Fetch helpers
directly. Its path is now:

```text
research question
      |
      v
WebRuntime.search()
      |
      v
dedupe sources
      |
      v
WebRuntime.fetch()
      |
      v
evidence pack
      |
      v
Codex synthesis
```

This makes Research provider-neutral. With TinyFish configured it uses
TinyFish Search + rendered clean Fetch; with the existing Brave/Tavily/Serper/
SearXNG configurations it preserves the previous local Fetch path.

## Routing rule

Prefer the least-powerful primitive that can complete the task:

1. **Search** when the task needs discovery or fresh sources.
2. **Fetch** when a URL is already known and only content is needed.
3. **Browser** when code must click, type, navigate, or preserve page state.
4. **Agent** when the workflow itself requires multi-step decisions.

Human Takeover remains a separate safety boundary. Browser or Agent providers
must not bypass Conveyor's existing human-only handling for secrets, payment
details, CAPTCHA, identity verification, or consent-sensitive steps.

## Next migration steps

New web-oriented workflows should construct a `WebRuntime` rather than import a
provider directly. The next safe migrations are chat grounding and topic watch.
Browser provider selection should remain a separate change because it crosses
into Computer Use and Human Takeover policy.
