# Main (mother-bot) Router — PR 5

The **default-off** flag `CONVEYOR_MAIN_ROUTER=1` requires
`CONVEYOR_AGENT_CONTEXT=1`, `CONVEYOR_AGENT_MAILBOX=1`,
`CONVEYOR_AGENT_DELEGATION=1`, `CONVEYOR_NAMED_AGENTS=1` and
`settings.agents_enabled`. With the router off, normal dispatch and
selected-agent conversations are unchanged. Both channel dispatch and Web
Console `POST /api/chat` invoke the same safe routing step before ordinary
chat, sending human approval cards through their existing transports.

## Owner routing precedence

Only authenticated owner messages addressed to the built-in `default`
Agent are considered. Authentication is checked before routing; a Feishu
operator requires a configured Feishu allowlist. Group messages and
attachments are excluded.

1. `!main` at the beginning opts out for that message. Already-selected
   named Agent conversations keep their existing direct handling.
2. Explicit owner selection (`@AgentName task` or `ask AgentName ...`)
   takes precedence over persona heuristics. Missing/archived/unauthorized
   Agents are **never** silently replaced with another.
3. Otherwise a deterministic server-side filter considers only active named
   Agents belonging to the same canonical Web operator whose current tool
   allowlist and global exposure cover all identified required capabilities.
   It intersects name/persona keywords with the message **after removing
   common conversational stopwords**, and requires at least **two distinct
   keywords** for implicit routing. Zero or one match stays with main;
   a unique highest-scoring eligible Agent routes deterministically.
4. Equal top matches can be disambiguated by one short chat completion.
   Its only valid response is structured JSON `{"agent_id":"...",
   "confidence":0.9}` referring to an ID in the **filtered** candidate
   set. Unknown IDs, malformed JSON, errors, disabled chat model or
   confidence below 0.85 stay with main.

The model's input and all task bodies are **untrusted data**. A model
prediction does not grant tools, write authority or permission to execute.
Every routing decision logs **only** Agent ID and reason, never message
body, transcript or model prompt.

## Existing delegation and human approval

The router proposes an internal `main.route_delegate` WRITE action using
Conveyor's original confirmation mechanism. This internal action is never
offered as a model-callable tool. Until an authenticated human approves the
exact original pending action, no task is queued. The server binds the
original approval to its original channel/operator/chat and re-checks
authorization when confirmed.

PR 4's `agent.delegate` accepts only Web Console owner-scoped mailbox
senders. After an owner confirms on Web, Telegram or Feishu, the router
maps **only this approved request** to the canonical Web default Agent
context and invokes that same `agent_delegation.enqueue()`, using its
scoped human approval token. The target ID and requested capabilities
are revalidated on confirmation and **again** by PR 4's worker.
Sender/recipient/global intersection, child depth 1, token/tool-call
budgets and stop cancellation remain enforced. The routed text can never
act as an approval or expand child tools.

## Returning results

A small `main_router_routes` table persists the approved action's
opaque idempotency key, target ID/name and origin channel/operator/chat.
**No user message bodies are stored in routing metadata.** After the
delegation worker atomically enqueues its single `kind=result` reply
and ACKs, the result bridge checks that committed status and records a
name-labeled result in the original owner's durable conversation.
The body is HTML-escaped, explicitly marked untrusted and without
instruction/approval authority. It also emits the existing completion
event for browser clients; the result is not a system or user message.

Web clients receive the result through the existing transcript/event
view. Telegram/Feishu origins are recorded in their durable transcript,
not proactively pushed to those chat transports by this PR; outbound
channel delivery needs separate channel adapter integration.

An authenticated `/cancel` or emergency stop from any channel invokes
PR 4's canonical `cancel_all`. A stopped or unsuccessful task cannot be
published as a successful routed result. The owner can use `!main` to
keep a particular request on the original main path.

## Tests

`tests/test_main_router.py` covers flag-off/dependencies, explicit
selection, archival and opt-out, structured model candidate rejection,
ambiguity, injected body/capability denial, real human confirmation,
cross-channel owner binding, duplicate result prevention and stop fencing.
