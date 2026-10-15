# Acknowledged Agent result outbox

This delivery upgrade uses the **existing** `CONVEYOR_AGENT_CHANNEL_ROUTING=1`
switch together with AgentContext + Mailbox + Delegation. No new user-facing
feature flag. With these switches off, the previous channel result path
and all other Agent routes behave as before.

## Atomic commit and restart recovery

`agent_delegation_queue.complete()` now adds a row to
`agent_result_notifications` in its **existing BEGIN IMMEDIATE transaction**
alongside `agent_mailbox.kind=result` creation and task ACK. The router's
existing `main_router_routes` records identify the **original, approved**
Telegram/Feishu destination. Only matching agent IDs and configured
owner/operator can produce a notification. Row identity is unique by
`(result_id, channel, chat_id)`. The notification text is redacted and marked
untrusted. No unapproved Agent output is sent.

If the process crashes immediately after the transaction commits, the
notification is still `pending`; channel polling at restart sends it.
The old `main_router_results.deliver()` no longer inserts a second
legacy notification when the new outbox is configured.

The old `agent_channel_result_outbox` rows remain intact and are drained
with their **existing at-most-once semantics** for backward compatibility.
New results are delivered through `agent_result_notifications`.

## Delivery and deduplication

The Telegram/Feishu process polls and obtains a fenced SQLite lease
(`leased`, random `lease_token`, 45s TTL) before send. On API success,
it records `state=sent`, `platform_message_id`, `sent_at`, and clears the
lease. Successful rows are not sent again.

On transient failure or unknown outcome, the row returns to `retry`
with exponentially increasing backoff (2, 4, 8, 16 seconds; capped at
300s), up to **5 attempts**. Expired leases from a crash are reclaimed.
Exhausted or recipient-revoked rows become `dead` and emit a
redacted Agent security audit event without chat text or addresses.
No busy loop, no infinite retries.

**Effectively-once rather than exactly-once:** The Telegram Bot API does
not provide a request idempotency key for `sendMessage`. If Telegram accepts
a send but its response is lost before the SQLite ACK, retry can produce a
second Telegram message. Every delivery carries an identical short stable
`[result:<first 12 chars of result UUID>]` marker, so the recipient can
identify duplicates; the marker is not authorization. The same residual
risk applies to Feishu when its selected send adapter does not expose a
server-enforced idempotency token. We store the returned Telegram/Feishu
message ID on acknowledged success, but this cannot retroactively suppress
an unknown successful prior send. Claims only deduplicate senders locally.

## Safety, kill switch and retention

Before each attempt both original recipient and Web owner egress approval
policies are checked via `egress_taint.requires_approval`. A denied egress
policy does not send; repeated refusal follows the same bounded retry/dead
policy, requiring human action to enable any allowed subsequent send.

The kill switch is tested before claiming **and immediately before send**.
A killed process keeps its polling task alive but stops consuming. Existing
pending/retry/leased rows survive and resume when the kill switch is cleared.
A send already in flight when kill activates cannot be undone; this is a
documented external API race, not a new approval path.

A bounded hourly cleanup removes `sent`/`dead` rows older than 14 days
(default policy). It never deletes unsent rows. For the Web Console, the
authenticated `/api/agents/kill` returns 404 when named Agent support is
off. For Telegram/Feishu, `/agentkill` is silently absent with those flags
off, while the switch remains reachable when killed but Agent features are
still configured.

## Checks

`tests/test_agent_result_outbox.py` exercises crash-after-commit recovery
without calling the legacy result bridge, transient delivery failures,
backoff, acknowledgments, deduplicated success, kill pause/resume,
expired leases, attempt caps, revocation/audit, egress refusal, retention,
and disabled feature behavior.

CI all-flags already enables `CONVEYOR_AGENT_CHANNEL_ROUTING=1`;
the new outbox uses that existing flag.
