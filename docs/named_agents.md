# P3-2 named-agent policy extension

The existing `agents.py` named agents, separate memory ownership, agent-owned routines,
workspaces and desktops already exist on main. This opt-in extension adds
**per-agent personas, memory scopes, tool allowlists and transcript messages**
without introducing a competing AgentStore.

## Flag and compatibility

`CONVEYOR_NAMED_AGENTS=1` enables the extension (default **off**); it
requires the existing `CONVEYOR_AGENTS_ENABLED=true` named-agent feature. With the flag off:
no extra metadata migration, no new tool schema, no agent-message dispatch,
and the legacy `/tools` list and API behavior are unchanged. The existing
`/api/agents` create/update endpoints accept the new fields only when the
flag is enabled; no new URL is registered, so unknown named-agent APIs remain 404.

## Storage and administration

The existing `state/job_queue.sqlite3` `agents` table is extended in place
with `persona TEXT`, `memory_scope TEXT`, and `tools_allowlist TEXT`
(JSON array). Existing fields `id`, `name`, `instructions`, `color`,
`workspace_path`, `display` remain intact.

Update an agent through the already authenticated `PUT /api/agents/<id>`:

```json
{
  "persona": "Technical reviewer",
  "memory_scope": "private",
  "tools_allowlist": ["notes.search", "memory.search", "routine.list", "agent.message"]
}
```

The allowlist uses **exact tool names**, not wildcards; default `[]`
means no chat tools. It is checked at both model schema generation and
execution (including after an operator confirms a WRITE). Existing global
danger levels, network permissions, egress-taint checks and approvals continue
to apply. A named agent cannot choose another agent's memory scope: nondefault
agents use `private` and the built-in `default` retains operator memory.

The `persona` is applied through the existing agent instruction system.
Scheduled checks and long-term memory use the existing agent-specific keys.
From an agent conversation, routine list/pause/resume/delete/run operations
cannot touch routines belonging to another agent.

## Agent-to-agent messages

The opt-in `agent.message` tool is **WRITE** and requires approval.
Supply a JSON string with exactly `{"to":"<agent-id>","text":"..."}`.
Only an existing, unarchived other agent can receive it. After approval,
the same unified transcript store records one `agent_message` in the
receiver's conversation (as untrusted tool data) and one in the sender's.
Neither agent receives new tools or execution authority from a message;
messages are not automatically interpreted as a new privileged user request.

## Scope of protection

This is an application-level allowlist for the **chat tool loop and contextual
tool dispatch**. It does not sandbox the underlying Unix user, grant access to
a different memory database, or independently restrict privileged direct
admin/maintenance operations without a chat identity. Use OS-level isolation
for untrusted agents.
