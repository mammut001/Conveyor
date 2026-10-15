# Messaging V2: manual inbox and approved replies

PR 3 extends the canonical AgentContext and durable `agent_mailbox` from
PRs 1–2. The feature is enabled **only** when
`agent_mailbox.enabled(settings)` is true: the mailbox flag and its
AgentContext dependencies must be enabled together.

## Backward compatibility

With `CONVEYOR_AGENT_MAILBOX` **off**, `agent.message` still writes
the same legacy agent transcripts with exactly the old JSON
`{"to":"<agent_id>","text":"..."}` and result
`agent message recorded`; neither `agent.inbox` nor
`agent.reply` is exposed. No mailbox schema is created.

With the mailbox **on**, `agent.message` enqueues kind `message` in
the durable mailbox. It does **not** write to the recipient transcript
or auto-run the recipient. Existing per-Agent `tools_allowlist`,
AgentContext, human approval and private-data egress checks remain in
force. The optional `idempotency_key` may be passed explicitly.
An approval token supplies a stable key per human-approved action,
so replay of the same approval is idempotent while a newly approved
repeat is a separate action. Internal explicitly approved direct
calls without a key use a deterministic per-session content hash.

## Tool contracts

- `agent.message` (WRITE, confirmation required):
  `{"to":"<agent_id>","text":"body","idempotency_key":"optional"}`
- `agent.inbox` (READ, own messages only):
  `{"limit":10,"status":"queued"}` (both properties optional).
  The output is XML-marked `untrusted=true` with the bodies HTML-
  escaped and explicit `instruction_authority=false` and
  `approval_authority=false`. Mailbox rows themselves carry the same
  untrusted/tainted labels. Reading an inbox also marks egress taint
  on the recipient session when that security policy is enabled.
- `agent.reply` (WRITE, confirmation required):
  `{"message_id":"<32-char mailbox ID>","text":"reply"}`.
  The parent must be a depth-0 message addressed **to the caller**
  under the same operator. The reply destination is derived from the
  original `from_agent`, not chosen by caller arguments. Replies have
  `parent_message_id` and depth=1; replies to replies are refused.
  `agent.reply` is classified as outbound for egress-taint gating.

**Never** feed message content into system or user prompt roles or treat
it as instructions, a capability grant, or an approval. The human
must expressly approve each WRITE operation. Confirmation-time
allowlist revalidation still applies, including if an Agent was
archived or its permissions were revoked after the initial preview.

PR 4 is responsible for any bounded background worker or mailbox
delivery semantics. No worker, auto-delivery or implicit task execution
is included here.
