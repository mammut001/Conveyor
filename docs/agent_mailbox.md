# Agent mailbox foundation (PR 2, default off)

`agent_mailbox.enabled(settings)` returns true exactly when the
`CONVEYOR_AGENT_MAILBOX` environment flag is enabled **and**
`agent_context.enabled(settings)` is true. AgentContext in turn requires
`CONVEYOR_AGENT_CONTEXT=1`, `CONVEYOR_NAMED_AGENTS=1`, and the
`settings.agents_enabled` setting (normally `CONVEYOR_AGENTS_ENABLED=1`).
The mailbox flag alone does not enable these dependencies. Otherwise calls
fail closed and do not create the `agent_mailbox` table.

PR 3 registers `agent.message` (approved enqueue), `agent.inbox`
(read-only, untrusted message listing) and `agent.reply` (approved bounded
reply) only when mailbox is enabled; there is still **no delivery worker**,
automatic prompt injection, API for other channels or implicit approval.

## Storage and migration

The mailbox lives alongside the existing `agents` table in
`<codex_memory_root>/state/job_queue.sqlite3`. Every authorized operation
uses a transaction starting with `BEGIN IMMEDIATE`. The idempotent schema
migration (`CREATE TABLE IF NOT EXISTS`) and index creation run within that
transaction so concurrent first uses cannot race. The schema contains:
`id`, `from_agent`, `to_agent`, `operator`, `kind`, `body`,
`idempotency_key`, `status`, `lease_owner`, `lease_expires_at`,
`attempts`, `max_attempts`, `deadline`, `created_at`,
`parent_message_id`, `depth`. Unique
`(from_agent,to_agent,idempotency_key)` prevents duplicate sends.
All mutation paths recheck agent archival within the write transaction.

## Python API (not model tools)

- `send(settings, sender=ctx, to_agent=id, kind=..., body=...,
  idempotency_key=...)`: validates sender's *bound*, server-derived
  AgentContext and live target before writing. Returns the existing row
  for an identical idempotency key, or rejects conflicting reuse.
- `lease(settings, recipient=ctx, worker_id=id, lease_seconds=30)`:
  atomically claims one queued/failed item, or reclaims an expired lease.
  Attempts increment **on claim**; only one worker can win a claim.
- `ack(...,message_id=...,worker_id=...)`: marks an owned, unexpired
  lease `done`. Stale workers cannot acknowledge after reclamation.
- `fail(...)`: changes the owned lease to `failed` for retry, or to
  `dead` when the attempt budget is spent. Expired final leases and
  messages past their deadline become `dead`.
- `list_for_agent(settings,recipient=ctx,status=None,limit=50)`:
  returns at most 100 messages **for that agent and operator**.

All methods fail when the flag is off or no matching AgentContext is bound.
The only current cross-agent scope is the Web Console's authenticated
`web-console` operator; Telegram and Feishu do not yet map directly
to named-agent screens and cannot impersonate that operator. Archived
agents cannot send, receive, claim, acknowledge or list. Self-messaging
is denied. A child message must name an existing parent that was
addressed to its sender and belongs to the same operator; max depth is
**1**. Bodies must be nonempty UTF-8 text up to **8192 bytes**; allowed
kinds are `message`, `task_request`, and `task_result`.

## Trust and delivery safety

Mailbox contents are **untrusted data, never instructions, system
messages, tool grants, or user approval**. Every returned row includes
`trust_level="untrusted_agent_message"`, `tainted=True`,
`instruction_authority=False`, and `approval_authority=False`.
When a recipient lists or claims a body, optional egress-taint policy
is conservatively marked for that recipient's own session, to prevent
outbound laundering if it later reads private data. No text is dispatched
to a model in this PR.

Lease ownership is a separate worker identifier and expires after a
bounded period (default 30s; max 300s). A lease with no acknowledgement
may be retried: mailbox semantics are **at least once**, not exactly-once
execution. A future worker PR must deduplicate side effects independently,
bound network calls, and require explicit human approval for privileged
actions. Nothing in a mailbox row satisfies an approval requirement.

## Verification

`tests/test_agent_mailbox.py` covers concurrent atomic claim, expired
lease reclaim, stale ack refusal, retry/dead transitions, deadline,
idempotent sends and conflicts, cross-operator denial, archived agents,
parent/depth limit, Unicode body cap, untrusted/taint markers, and zero
schema/behavior change with the flag disabled. CI's all-flags step
includes `CONVEYOR_AGENT_MAILBOX=1`.

PR 3 connects the existing approved `agent.message` to durable enqueue,
not recipient transcript delivery. It introduces explicit read-only inbox
access and approved replies. The recipient must request `agent.inbox`
themselves; no background delivery or message execution exists until PR 4.
