# Agent delegation worker — PR 4 (default off)

Enable `CONVEYOR_AGENT_DELEGATION=1` **with**
`CONVEYOR_AGENT_MAILBOX=1`, `CONVEYOR_AGENT_CONTEXT=1`,
`CONVEYOR_NAMED_AGENTS=1`, and existing `settings.agents_enabled`.
Disabling any prerequisite removes `agent.delegate` from chat schemas,
disables task claiming and suppresses worker startup. There is no default-on
behavior, new network API, or unsolicited prompt injection.

## Durable schema and compatibility

PR 2's `agent_mailbox.kind` CHECK originally allows
`message/task_request/task_result`. The gated
`agent_delegation_schema.ensure()` migration rebuilds the table under
`BEGIN IMMEDIATE`, adds **task/result**, preserves all previous columns,
rows, indexes and parent links, and validates foreign keys. Migration only
occurs on a delegation-enabled mailbox operation; off is unchanged.
The worker claims **only** `kind=task` messages, never ordinary inbox
messages, and returns `kind=result` linked to the original task.

## Human-approved tool

`agent.delegate` accepts exactly
`{"to":"<agent_id>","task":"...","tools_subset":["notes.search"]}`.
It is **WRITE**, requires a fresh human approval through the existing
approval relay, and is treated as outbound by the existing egress-taint
policy. The confirmed action carries an idempotency key tied to its
approval token, and a scoped confirmation context is required even if
internal callers set `egress_approved=True`.

The receiver must be active, have the same operator identity and cannot
equal the sender; the task is a nonempty UTF-8 string of at most 3500
bytes. Tasks are root messages (depth=0); child execution cannot spawn
another delegated task (effective child depth=1).

## Execution / trust boundaries

The worker runs under the receiver's freshly validated AgentContext,
with capabilities **intersected** across the sender, receiver and globally
exposed tool registry, then intersected again when leasing/executing.
`agent.delegate`, `agent.message`, `agent.reply`, recursive subagents
and parallel agents are never delegated. Task text is transported in a
JSON-quoted `untrusted_task_data` field; it is never system instructions,
an operator confirmation, or a capability grant.

The model is limited to three tool calls and an aggregate generation
budget of at most 1200 completion tokens, a maximum of 80 seconds per
turn and the earlier lease/deadline. Tools outside the intersection
are denied. Any WRITE/WRITE_SAFE or tainted outbound action is **never**
executed by the worker; it is sent through the existing human approval
inbox (when enabled), or blocked and reported to the sender. A human
must approve it separately.

Two simultaneous task leases per operator at most are enforced under
`BEGIN IMMEDIATE`, including across Web Console processes. The worker
starts only from the Web Console's existing startup path with the
delegation flag on, and is cancelled at shutdown. The authenticated
Web Computer Stop, channel emergency stop and /cancel also stop
delegated tasks and mark outstanding task messages `dead`, so a stop
cannot immediately requeue the same task.

A successful turn **enqueues a single** `kind=result` reply with
idempotency key based on the source task ID, **before** acknowledging
the lease. On a crash between those steps, expired leases are
reclaimable; retries reuse the same reply identity instead of
duplicating messages. Failures call mailbox.fail for normal retry/dead
transitions. Every result remains untrusted data in the recipient's
inbox, not a system/user message or an approval.

## Limitations

This is a depth-one, bounded chat-model task runner only. It is not a
general background Codex shell/desktop executor; existing human
approvals must authorize any privileged side effects. The existing
mailbox only maps named agents through the Web Console operator.
Telegram/Feishu mapping and broader organization routing come in
later phases. Browser/provider network timeout limitations of the
existing chat client remain in effect.

Tests cover flag-off isolation, approval/egress, capability narrowing,
injected task content, same-operator concurrency, lease reclaim,
bounded tool calls/deadlines, once-only result creation and emergency
stop.
