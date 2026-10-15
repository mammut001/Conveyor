# Multi-Agent Security Hardening (PR 8)

This is the final default-safe hardening layer over PR #138–#144.
No Agent request text, mailbox bodies, prompts, channel/chat IDs, or
approval-token contents are stored in the security audit table.

## Threat model / observed gaps

1. **Unbounded mailbox growth / spam:** previously each distinct idempotency
   key could add more messages and delegated tasks without a per-operator
   quota or recipient pending-message cap. Exhaustion could contend for the
   SQLite queue and create persistent costs.
2. **Unbounded retention:** finished/expired/dead task envelopes and results
   could remain indefinitely. These contain sensitive user/task data.
3. **Limited forensic attribution:** existing stdout logger recorded route
   decisions, but no bounded Agent/operator-scoped durable audit of bind,
   delegate, cancel and deny events existed.
4. **No hot global kill:** individual flags control startup and feature
   exposure, but cannot atomically disable channel binding, routing,
   enqueues and running delegated children through one operator switch.
5. **Named-context residual tool access:** hiding `agent.message` alone
   would still leave ordinary named Agent tools visible/executable. Kill
   must check the AgentContext for *all* tools.
6. **Result bypass of recipient queue limit:** child output is inserted in
   the same lease transaction, not by `agent_mailbox.send`. It also needs
   the queue-cap check before result insertion.

## Admission controls

The existing mailbox **BEGIN IMMEDIATE** transaction checks idempotency
first, then denies new `message`, `task`, `task_request` and
`task_result` enqueues when either:
- `CONVEYOR_AGENT_RATE_PER_HOUR` (default 60, per canonical operator,
  sliding 3600-second window) is reached; or
- `CONVEYOR_AGENT_MAX_PENDING` (default 100, per operator/recipient,
  statuses queued/leased/failed) is reached.

Limits are runtime env-configurable and clamped to safe ranges. An exact
idempotent retry consumes no additional capacity. Worker-generated
`kind=result` replies bypass the *owner's submission rate* but still
respect the recipient pending cap, inside the same atomic completion/ACK
transaction. A denied result cannot be committed as a successful task.

Routing enqueues go through PR 4's human-approved delegation and the same
atomic admission checks. No route-model choice is itself an approval.

## Bounded retention

The Web Console starts one periodic cleanup loop only when mailbox is
enabled. It checks hourly; each tick removes at most 100 old mailbox rows
and 100 old audit events. Only `done`/`dead` mailbox rows
are purged; even stale `queued`/`leased` messages, tasks and results are
retained. `CONVEYOR_AGENT_RETENTION_DAYS` defaults to 14
(clamped to 1–365 days). `purge_once` is also callable independently
for deterministic tests.

Foreign keys remain ON for normal transactions; deletion removes child
result rows first and only removes a parent after all child references
are gone. Active task/message rows are retained unless they are old
result-kind rows explicitly covered by the retention policy. This is
a row purge, **not a forensic secure erase** of SQLite WAL, old backups
or filesystem blocks. Operators must handle storage-level backups and
checkpoint/vacuum through their existing data-retention policy.

## Agent audit

`agent_security_audit` stores `happened_at`, canonical `operator`,
`event` in `route/delegate/bind/cancel/deny`, valid `agent_id`, and
one server-controlled reason from a strict allowlist. No freeform strings
or task payloads enter the table.

The authenticated Web Console can GET
`/api/agent-screens?agent_id=<id>&view=audit` (with existing screen
flags enabled). The exact AgentContext and operator scope is verified
before SQL selection; there is no cross-agent/operator audit listing.

## Emergency kill

Set `CONVEYOR_AGENTS_KILL=1` to suppress:
- mailbox messages, delegation, route decisions and channel binding,
  including queued outbound routed result sends;
- `agent.message`, `agent.inbox`, `agent.reply`,
  `agent.delegate` and internal `main.route_delegate` tools;
- all other tools offered/executed within **non-main named Agent**
  contexts (so an obsolete tool schema cannot escape the kill);
- new child lease acquisitions; the running worker notices the switch,
  marks unfinished tasks dead and cancels in-flight tasks.

**The kill flag is intentionally NOT enabled in CI all-flags**;
dedicated tests toggle it at runtime. Authenticated /cancel/emergency
stop intentionally bypasses the killed feature gate to invalidate live
leases. The default/main operator chat and its non-Agent tools remain
available. Turning the flag off does not resurrect dead tasks.

The app reads BOTH the process environment and the runtime kill marker
`CODEX_MEMORY_ROOT/state/agents_kill` on **every** security-sensitive
request. The environment value `CONVEYOR_AGENTS_KILL=1` always wins.
The runtime marker is installed atomically and may be changed without
restarting the daemon:

- Web Console: authenticated `POST /api/agents/kill` with JSON
  `{"enabled":true}` or `{"enabled":false}`, same-origin and
  `X-Requested-With: XMLHttpRequest` headers. Anonymous and
  cross-origin requests are rejected.
- Authenticated Telegram/Feishu owner private chat:
  `/agentkill on` or `/agentkill off`. Non-owner/group commands
  cannot change the switch. Existing emergency stop and `/cancel`
  still work while killed.

Every authorized switch mutation is audited with fixed reason
`kill_on` or `kill_off`, without chat content. On kill, active child
leases are invalidated and running children cancelled once; the worker
remains alive, polling at a low rate, and resumes on unkill without
restarting. Previously killed tasks remain `dead`; they never resume.
Clearing the runtime marker cannot override an active environment kill.

The retention loop starts only in the **primary Web Console** process
when Mailbox is enabled; workers hosted without a Web Console do not
currently start autonomous retention. Those deployments must run
`agent_security.purge_once(settings)` from their own scheduled service
until retention startup is unified. Never run competing uncontrolled
retention loops.

## Known residuals

External channel delivery remains *at most once* under uncertain
network timeouts; delivery failures may lose a notification instead of
duplicating a result. Route-result transcript publishing is also
at-most-once after its durable delivered claim, and can lose a visible
notification on a process crash between claim and write. Moving to an
acknowledged outbox with provider-specific idempotency is follow-up work.

The separate permanent original SQLite/backup/WAL artifacts are outside
the retention job's secure erasure guarantee. Sensitive contents always
remain untrusted data; no Agent result grants an action approval.
