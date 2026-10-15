# Web Console: per-Agent screens (PR 6)

This is a presentation layer over the existing AgentStore, AgentContext,
TranscriptStore, memory API and mailbox/delegation queues. No new Agent
identity or permission registry is introduced.

## Activation

- `CONVEYOR_NAMED_AGENTS=1`, `CONVEYOR_AGENT_CONTEXT=1` and
  `settings.agents_enabled=true`: each current (non-archived) Agent gets
  the tabbed workspace; disabled/missing switches render the old ChatPanel.
- Inbox appears only with `CONVEYOR_AGENT_MAILBOX=1`.
- Tasks appears only with `CONVEYOR_AGENT_DELEGATION=1` (which needs Mailbox).
- Chat uses existing fixed per-Agent `session_id`, and Memory uses
  `/api/memory?agent=<id>` (main maps to existing Web operator owner).
  Main routed results remain labeled by PR 5's existing transcript path.

Existing global Console Tasks/Inbox controls remain unchanged; the additional
Agent-local Inbox and Tasks are scoped projections, not the global queues.

## API

All endpoints require the existing Web Console bearer token. Each request
requires explicit `agent_id`, re-resolved through AgentContext for the
canonical `web-console` operator and rejected if missing/archived/spoofed.

- GET `/api/agent-screens/config?agent_id=...`: enabled tab flags.
- GET `/api/agent-screens?agent_id=...&view=inbox`: incoming
  `message/task_request/task_result` mailbox messages for that Agent.
- GET `/api/agent-screens?agent_id=...&view=tasks`: bounded list of
  delegated tasks SENT or RECEIVED by that Agent, including
  queued/leased/done/failed/dead status. Task bodies are never returned.
- POST `/api/agent-screens/cancel` JSON
  `{"agent_id":"...","task_id":"..."}`: only the named Agent's specific
  live task transitions to dead. Requires bearer auth, AJAX header,
  JSON Content-Type and same-origin fetch checks. The lease fence and
  running child cancel use PR 4's existing task table and worker.

The entire request is rejected with 404 if the prerequisite feature is
disabled, without exposing mailbox/schema data.

## Injection and cancellation

All mailbox bodies are **untrusted** and displayed using React JSX text
children (`<pre>{body}</pre>`). Never interpolate into HTML, a Markdown
parser, iframe, URL or execution prompt. Incoming text never grants
AgentContext, approval, or task execution authority.

Task cancellation uses `operator=web-console`, `kind=task` and the
specified `agent_id` as source or recipient, updates just that task under
SQLite's write lock, and cancels its in-flight asyncio task via its owning
event loop. Global emergency stop is not used. Already-done tasks are not
reopened; a cancelled task cannot commit a delegated result.

Tests: `tests/test_web_agent_screens*.py` (identity, disabled flags,
HTTP auth, single task cancellation, CSRF) and
`web/e2e/agent-screens.spec.ts` (actual browser escaping, tab gate,
task cancel).
