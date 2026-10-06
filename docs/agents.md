# Agents

One conversation, one agent. With agents on, the Web Console's sidebar lists
named agents instead of sessions; each has standing instructions and exactly
one conversation that is always there.

This is being built in phases. Phase 1 is identity and prompts; phase 2 gives
each agent its own always-on desktop. Workspace, memory, schedules and
parallel execution follow; until then those remain shared.

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

## Agent desktops (phase 2)

```dotenv
CONVEYOR_AGENT_DESKTOPS_ENABLED=true     # needs CONVEYOR_AGENTS_ENABLED too
CONVEYOR_AGENT_DESKTOP_SIZE=1440x900
```

Every agent except `default` gets its own virtual desktop that is always on:

```text
conveyor-agent-desktops.service  (agent_desktops.py, one supervisor)
   ├─ agent A  →  Xvfb :101 + xfwm4 [+ browser, profile A]
   ├─ agent B  →  Xvfb :102 + xfwm4 [+ browser, profile B]
   └─ …
```

- **Always on.** The supervisor starts a display for each agent, restarts it
  if it dies, and brings everything back after a reboot. The desktops outlive
  the supervisor itself (`KillMode=process`; a restarted supervisor adopts
  what is running), so a deploy does not close any windows.
- **Its own browser profile.** Logins, cookies and tabs belong to one agent.
  The browser is started the first time the agent's screen is opened (or with
  **Open browser** in the viewer) and then left running; one the operator
  closes stays closed until asked for again.
- **Its own screen and takeover.** The Computer tab shows that agent's
  display. Taking control holds a lease scoped to that desktop
  (`agent:<id>`), so it pauses nothing on the host desktop or on other agents.
- **Private.** Each display has its own X authority cookie (mode 0600) and
  does not listen on TCP; desktop programs get a minimal environment with
  none of the deployment's secrets.
- The `default` agent keeps using the host's own desktop session.
- Removing an agent stops its desktop and frees its display number. Its
  browser profile is left on disk.

The Web Console never starts desktop programs: it records what it wants (the
agents table, a small request file) and the supervisor acts on it. The
supervisor's unit is intentionally not sandboxed like the others, because
snap-packaged browsers need a setuid helper and a writable home.

**Sizing** (measured on a 2-core ARM host): about 220 MB for an empty
desktop and about 530 MB more with Firefox open. Memory allows roughly ten
agents with browsers on a 12 GB host; CPU is the real limit — more than two
or three agents actively driving their screens will feel slow.

**Not yet:** the Agent's own computer use still runs on the host desktop.
Routing it to the agent's desktop is the next step; until then an agent
desktop is for the operator to watch and drive.

**Isolation is by display and browser profile only.** All agents run as the
same Linux user and share one filesystem.

## Safety notes

- Instructions are operator-authored and shape the role, but the chat tier's
  numbered rules (no invented facts, write tools need confirmation, untrusted
  tool output) come after them in the prompt and still apply.
- List previews are redacted like any other text leaving the console.
- All agents still share one workspace, one memory and one job at a time.
