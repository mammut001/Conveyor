# PR 7 — Telegram / Feishu private Agent channels and Chinese main routing

Default OFF: `CONVEYOR_AGENT_CHANNEL_ROUTING=1` additionally requires
`CONVEYOR_NAMED_AGENTS=1`, `CONVEYOR_AGENT_CONTEXT=1` and the existing
Agent system to be enabled. Off means no new commands, owner bindings,
result senders or persistence tables.

## Private owner commands

In Telegram and Feishu one-to-one owner chats:

- `/agents` lists only active Agents and a short persona preview.
- `/agent <name|id>` binds the *current channel/chat ID*, persisted in
  `state/job_queue.sqlite3` with the configured human operator ID. The
  binding survives restarts. Names are exact, case-insensitive; ambiguous
  names are rejected. Unknown or archived agents cannot be selected.
- `/agent main` clears the binding.

Only the configured Telegram owner ID / explicitly configured Feishu open
ID may use these commands, and only for `chat_type=p2p`.
Group chats, unauthenticated identities and Feishu bootstrap mode never
bind. An archived existing target immediately falls back to main.
`stop computer` and `/cancel` are handled before this routing; they
preserve the existing emergency cancellation semantics.

Selected Agent replies are labeled `[AgentName]`. The original channel,
chat ID, sender ID, and original OutboundPort remain intact. AgentContext
resolves to the selected Agent with its current tool allowlist and private
memory owner; no user-supplied text changes capabilities. The chat model's
short-term history key uses the same durable Web Agent session key as
its Web screen. Transcript turns are also mirrored to that Agent's existing
Web session (the original channel transcript remains intact).

## Result delivery and security

When an approved PR5 routed task atomically completes, the existing
`main_router_results.deliver()` writes its owner transcript and adds a
single durable outgoing record keyed by task ID. Telegram and Feishu bot
processes each run one low-frequency consumer using their *existing live*
outbound API (TelegramChatOutbound, FeishuChannel plain-text send).

A consumer transactionally claims a result **before** making a network
call and checks the original owner against current channel allowlists.
Egress-tainted results requiring approval remain blocked; an automatic
send never grants itself approval. The result is name-labeled and explicitly
untrusted plain text (not an instruction, approval, or HTML card).

**Exactly-once caveat:** Telegram/Feishu do not give this implementation
a transactional remote idempotency key. The durable outbox guarantees
*at most one send attempt*, even after process restart or timeout; it
does not guarantee eventual delivery when a request outcome is ambiguous.
Such items remain `unknown` and cannot be automatically retried without
risking a duplicate. Investigate them manually.

The result consumer is created only when the PR7 flag is enabled; the Web
worker never impersonates an external channel connection. PR4 emergency
stops continue to fence delegated tasks, so stopped children cannot
create new success results.

## 中文关键词

PR5 implicit main routing now tokenizes Chinese characters into 2-character
overlapping bigrams, filtering generic terms like `帮我`, `一下`,
`可以`, `你好`, `今天`, `什么`, `怎么`, `助手` and `用户`.
It still requires at least **two distinctive overlaps** with an active,
capability-authorized Agent persona. No match, tie ambiguity, model
invalid candidate and low confidence still stay with main. Explicit
`@AgentName` continues to take precedence.

Run `tests/test_agent_channel_routing.py` and CI all-flags; keep PR open
for review, never deploy or merge before approval.
