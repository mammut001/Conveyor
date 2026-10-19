# Chat request deadlines and tool feedback

The shared Telegram, Feishu, and Web chat client now uses cancellable async HTTP
requests. `chat_timeout_seconds` bounds a whole completion request, including a
stream that keeps sending heartbeats or an unfinished JSON body that arrives a
few bytes at a time. Closing a streamed reply or cancelling a pending request
closes its provider connection. The streaming reader has a bounded queue and
is cancelled and awaited when its consumer exits.

The HTTPX version is pinned explicitly in `requirements.txt`; the same version
was already present through the Telegram dependency. OpenAI-compatible request
payloads, tool schemas, SSE text deltas, non-streaming fallback responses, HTTP
status errors, and provider error redaction retain their existing interfaces.
Tool-enabled replies share one configured model-wait budget across all rounds,
including the final answer. Time spent executing tools does not consume that
budget or cause running tools to be cancelled.

READ tool calls now publish their readable summary before execution, followed
by a status indicating that results are being turned into an answer. These are
progress updates, not final transcript turns. Write tools continue using the
existing confirmation flow.

## Verification

`tests/test_chat_client_lifecycle.py` runs against a loopback HTTP provider:

- Repeated SSE heartbeats cannot keep a request alive past its total deadline.
- A trickled, incomplete JSON response also reaches the total deadline.
- Closing a partially consumed stream disconnects the HTTP client.
- Cancelling a waiting stream or a non-streaming completion disconnects it.
- A provider error containing an opaque API key is redacted before being returned.

The deadline tests use a 0.5-second request limit and check that the call returns
within two seconds. Connection cleanup is checked at the server socket, rather
than inferred from a cancelled Python task. Tool-loop tests verify that progress
is visible while a tool remains blocked, and Web API tests verify status events
arrive before the final answer. The existing chat-tier smoke suite verifies
routing, history, non-streaming fallback, error handling, and guarded escalation.

No live provider performance claim follows from these tests. The local runtime
reported `chat_configured: false` on 2026-10-18, so live time-to-first-token and
answer quality could not be measured with its current configuration. These
changes have not been deployed to a production service.

Tool-loop regression tests also verify the shared model-wait budget cancels a
pending later round and excludes time spent in an actual tool. Agent context
tests cover clearing scoped history and recovering each Agent's `/deep` request
from persistent storage.
