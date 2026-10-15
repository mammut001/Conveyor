# P3-3 AgentContext foundation (default off)

`CONVEYOR_AGENT_CONTEXT=1` activates canonical, server-derived AgentContext
only when the existing `CONVEYOR_AGENTS_ENABLED=true` and
`CONVEYOR_NAMED_AGENTS=1` are also enabled. The flag defaults **off**:
existing short-term history keys, session files, tool lookups and legacy
channel behavior are unchanged when it is off.

## Identity and authority

`agent_context.AgentContext` records `agent_id`, `session_id`,
`memory_owner`, `operator_identity`, `origin_channel`,
`tool_capabilities`, optional `delegation_id` and
`trusted_owner_request`. Canonical identities are derived from the
authenticated transport's (channel, chat ID, operator ID) tuple and
the existing `agents` table. Unknown, archived or impersonated Web
agent identities fail closed; no fabricated Agent IDs, memory owners or
tool lists are accepted from model arguments.

The inbound Web/Telegram/Feishu dispatch and Web chat bind the resolved
identity with a task-local ContextVar. Tool-schema exposure and tool
execution (including approval replay) recheck the current agent's
allowlist and reject absent/mismatched identities. The context binding
follows asynchronous awaits and is reset at request completion, with no
cross-chat global shared state. Direct tool APIs must pass all three
identity fields; schema inspection with no identity returns no tools.

The default Agent retains pre-existing tools, but not `agent.message`.
Named Agent tool grants remain exact-name allowlists. Delegated permissions
are `requested ∩ sender tools ∩ recipient tools`; a delegated request
does not inherit additional tools, operator trust or memory access.
There is no delegation worker or message inbox in this PR.

## Session and memory separation

For **non-default agents**, the model's short-term SQLite chat key
includes canonical Agent ID and the full channel/operator/chat session
identity. Their JSONL session files use an `<agent_id>_` prefix so histories
cannot collide. The built-in **default agent keeps its legacy**
`channel:chat_id` key and unprefixed `channel_chat_id_operator_id.jsonl`
file name for Telegram, Feishu, and Web, even when this flag is enabled.
Enabling the flag therefore preserves existing default-agent chat history
and Codex session context; only non-default agents use the new namespace.

Both legacy long-term memory tools and Memory V2 use
`owner_for_chat()`, which requires canonical identity with the flag on.
Nondefault Agents only access `agent:<id>` memory; the built-in default
Agent continues to use the operator owner. Authenticated Web Console
memory administration still interprets an omitted `agent` as the built-in
default agent; an explicit named-agent selection uses that agent's own
verified memory scope. All Web memory endpoints continue to require their
existing operator authentication, regardless of the flag.

## Safety and compatibility checks

- Reject missing operator/channel/chat identity and archived Agents
  for regular actions. Exact Computer Use stop phrases are the narrow
  exception: after channel-owner authorization they cancel the active
  Computer Use task even if the agent chat was archived; they never run
  through agent tools or authorize new actions.
- Reject forged memory owners, mismatched bound ContextVars and enlarged
  delegated tool lists; recheck policy before executing confirmed actions.
- Check Agent A's memory cannot be read or written from Agent B.
- Test multi-agent flags on and off, plus all-flags CI.

This is an application-level boundary, **not** a process sandbox. All
agents still share a Unix account. The durable mailbox, worker execution
and mother-bot routing are subsequent PRs.
