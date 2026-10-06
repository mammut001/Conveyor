# Agents

One conversation, one agent. With agents on, the Web Console's sidebar lists
named agents instead of sessions; each has standing instructions and exactly
one conversation that is always there.

This is being built in phases. **Phase 1 (this document)** is identity and
prompts. Later phases give each agent its own desktop, workspace, memory and
schedule; until then those remain shared.

## Enable

```dotenv
CONVEYOR_AGENTS_ENABLED=true
```

Off by default. Turning it on changes nothing by itself: the built-in
`default` agent has no instructions until you give it some.

## What an agent is

| Field | Meaning |
|---|---|
| Name, color | How it appears in the list |
| Instructions | Its role. Added to the chat tier's system prompt and, as an `<agent-profile>` block, to every Codex job started from its conversation |
| Project folder | An absolute path on the host. Shown as a tag today; becomes the agent's own workspace in phase 3 |

Agents live in the `agents` table of `state/job_queue.sqlite3`, next to
sessions and the queue.

## Conversations

- An agent's conversation is the web session `web:web-console:agent-<id>`.
  The mapping is derived from the id, so no session row is rewritten and an
  agent's conversation exists (empty) from the moment the agent does.
- Telegram, Feishu and web sessions created before agents were turned on all
  belong to the `default` agent.
- Removing an agent archives it: the conversation and its jobs are kept but
  the agent leaves the list and its instructions stop applying. `default`
  cannot be removed.

## Web Console

- **Sidebar**: avatar, name, project tag, a preview of the last message, and
  a status dot — green while a job of its conversation is queued or running,
  amber ("Waiting for you") while one of its approvals is pending.
- **＋** creates an agent; **⋯** on a row, or **Edit agent** in Details, edits it.
- **Right panel**: Details (instructions, project, the selected job and its
  changes), Library (empty until phase 4), Computer (the live screen).

## API

```text
GET    /api/agents            {enabled, agents: [...]}   (enabled:false when off)
POST   /api/agents            {name, instructions?, workspace_path?, color?}
PUT    /api/agents/<id>       any subset of the above
DELETE /api/agents/<id>       archive
```

Mutations answer `409` while agents are off. To talk to an agent, send its
`session_id` to the existing `/api/chat` or `/api/tasks`.

## Safety notes

- Instructions are operator-authored and shape the role, but the chat tier's
  numbered rules (no invented facts, write tools need confirmation, untrusted
  tool output) come after them in the prompt and still apply.
- List previews are redacted like any other text leaving the console.
- Phase 1 does not isolate anything: all agents still share one workspace,
  one memory, one desktop and one job at a time.
