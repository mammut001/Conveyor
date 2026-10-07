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
| Project folder | An absolute path to a git repository on the host. The agent's jobs run there; shown as a tag in the list |

Agents live in the `agents` table of `state/job_queue.sqlite3`, next to
sessions and the queue.

## Conversations

- An agent's conversation is the web session `web:web-console:agent-<id>`.
  The mapping is derived from the id, so no session row is rewritten and an
  agent's conversation exists (empty) from the moment the agent does.
- Unbound Telegram chats, Feishu and older web sessions belong to the
  `default` agent. Telegram chats can explicitly bind an agent (see below).
- Removing an agent archives it: the conversation and its jobs are kept but
  the agent leaves the list and its instructions stop applying. `default`
  cannot be removed.

## Telegram project conversations

Enable `CONVEYOR_AGENTS_ENABLED=true`, then select a project in Telegram:

| Command | Action |
|---|---|
| `/agent` | Show the current agent and a paginated project picker |
| `/agent list [page]` | List agent IDs, project folders and selection buttons |
| `/agent <name or ID>` | Select an existing agent (names must be unique) |
| `/agent new Name \| /absolute/repository` | Create and select an agent for an existing Git repository root |
| `/agent new Name` | Create an agent that uses the configured workspace |
| `/agent reset` | Return to the original default conversation, preserving all agent histories |

In private chats you can switch projects and switch back to continue. In a
forum group, **each topic selects its own agent and has its own conversation**.
The general chat and different topics never implicitly inherit each other's
selection. Group commands such as `/agent@YourBot list` work; ordinary group
messages still need to mention or reply to the bot. Only the configured
operator can select or create agents or use the buttons.

Every selected agent has a separate history within that chat/topic. Jobs,
retained worktrees, chat memory, `/deep` requests and approvals keep the agent
that created them, even after selection changes or the process restarts.
Switching back restores that conversation. Telegram releases the inbound
update after enqueue, so long jobs do not block `/cancel` or project selection;
queued jobs are resumed when the Telegram service starts. Telegram history stays separate
from the agent's Web conversation; long-term memory, standing instructions,
project folder and desktop are shared with that agent, subject to the existing
memory feature/group-privacy settings. Agent instructions can be edited in Web.

`/status`, `/jobs`, `/last`, `/cancel`, `/diff`, `/apply` and `/discard` locate
jobs by conversation, channel and operator, independently of runner lanes and
the concurrency limit. Asynchronous job output includes the agent name and
job ID so a result arriving after a switch remains identifiable. `/git_status`
and file search use the selected repository; host diagnostics still inspect
the host. Reminders, routine reports, images, approvals and recovered task
output are delivered to their original topic.

Selections live in `agent_chat_bindings` in the queue database and survive
restarts. Telegram conversation addresses are `<chat>:topic:<thread>` for a
topic and append `:agent:<id>` for an explicit agent selection; only transport
adapters translate them to Bot API `chat_id` and `message_thread_id`.
Unbound private chats keep their original session IDs and default history.
`/agent reset` returns to that history without deleting project conversations.

Before first binding or switching a **legacy** conversation, its old queued
jobs and active worktree must be resolved: older jobs had no pinned agent.
New scoped jobs can remain queued/running while switching, because their
identity cannot change. If an agent is archived, new requests fail explicitly;
read-only task controls, cancellation and discard remain available. Agent
configuration/database failures cannot silently send a pinned task to the
default repository, desktop or memory. Old group jobs created before topic
isolation remain in their original group session and can be inspected in Web.

## Web Console

- **Sidebar**: avatar, name, project tag, a preview of the last message, and
  a status dot — green while a job of its conversation is queued or running,
  amber ("Waiting for you") while one of its approvals is pending.
- **＋** creates an agent; **⋯** on a row, or **Edit agent** in Details, edits it.
- **Right panel**: Details (instructions, project, the selected job and its
  changes), Library (what the agent has accumulated), Computer (its screen).

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
- **Private.** Each display has its own X authority cookie and does not
  listen on TCP, so only this Linux user's programs can see or drive it;
  desktop programs get a minimal environment with none of the deployment's
  secrets. The cookie is stored as an entry for that display number in the
  user's `~/.Xauthority`, because sandboxed (snap) browsers cannot read files
  under a hidden directory.
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

### The agent works on its own desktop

A desktop task started from an agent's conversation (`/computer_task …`, or a
plain request the router recognises as one) runs on that agent's display:

```text
conversation ─▶ task {takeover_scope: agent:<id>} ─▶ X11ComputerBackend ─▶ xdotool / import on :10N
```

- The host desktop is driven through kernel input devices, which every X
  server on the machine would receive. An agent's desktop is driven with
  XTEST (`xdotool`) and captured with `import`, both addressed by `DISPLAY`,
  so input can only land on that agent's screen (`desktop_x11.py`).
- The planner gets a simpler contract there: one full-screen screenshot per
  observation, click coordinates in its pixels, and type / hotkey / scroll go
  to whatever has focus. `cmd` in a shortcut is treated as `ctrl`.
- The browser is brought up before the first step if the desktop is empty.
- Steps go through the same request store as every other task, so the
  allow-list, blocked keywords, redacted trail and stop command all apply.
  The host's desktop node cannot see or claim them (`wrong_node`), and the
  agent's executor cannot claim the host's.
- **One task at a time per desktop.** A task on one agent's desktop does not
  block one on another's or on the host's.
- **Takeover is per desktop.** While you hold an agent's screen its task
  waits and re-observes when you release; nothing else is paused.
- Tasks from unbound Telegram chats, Feishu, the default agent and the one-shot
  `/computer_observe` / `/computer_action` commands still use the host
  desktop.
- An app allow-list (`CONVEYOR_COMPUTER_ALLOWED_APPS`), if you set one, is
  checked against the X window class on agent desktops (e.g. `firefox`).

**Isolation is by display and browser profile only.** All agents run as the
same Linux user and share one filesystem.

## Agent workspaces (phase 3)

An agent with a **project folder** runs its Codex jobs there instead of in
`CODEX_WORKSPACE_ROOT`. The folder must be the root of a git repository on
the host; the Details tab says so if it is missing or not one.

- **Worktrees** for its jobs are cut from that repository (still under
  `<task_root>/worktrees/`), and `/diff`, `/apply`, `/discard` act on it.
  The repository is resolved from the worktree itself and accepted only if
  it is the configured workspace or a registered agent folder, so a stray
  worktree can never redirect an Apply.
- **Apply checks the right repository**: "main workspace has uncommitted
  changes" refers to the agent's folder, and the patch lands only there.
- **Apply rules.** The path allowlist in `runner/apply_policy.py` describes
  Conveyor's own source tree and guards the configured workspace. In an
  agent's folder it does not apply — any layout is fine — but the deny list
  (secrets, `.git`, `node_modules`, virtualenvs…), the high-risk gate
  (workflows, deploy and auth files; `CONVEYOR_APPLY_ALLOW_HIGH_RISK`) and the
  untracked-file checks (no symlinks, binaries or oversized files) all do.
- An agent without a project folder, and unbound Telegram chats and
  Feishu, keep using the configured workspace.
- `/git_status` and file search use the selected agent workspace; host
  diagnostics still inspect the host.

## Memory, scheduled checks and the Library (phase 4)

- **Memory.** Every agent you create keeps its own long-term memory: facts
  remembered in its conversation are in its prompts and visible to its
  `memory.*` tools only. The default agent — and so unbound Telegram chats and Feishu —
  keeps using the operator's store. With an agent selected, the Memory view
  shows and edits that agent's facts (`/api/memory?agent=<id>`).
- **Scheduled checks.** A routine created while an agent is selected (or by
  that agent in its conversation) belongs to it. It runs as the agent — its
  instructions, its memory, its conversation so far — and the result is
  posted into the conversation as a message from the agent, so the list
  preview shows it. It still appears in the inbox and can still be delivered
  to Telegram or Feishu. Routines of the default agent are unchanged.
  Webhook-triggered runs get no long-term memory, as before.
- **Library tab.** What the agent has accumulated: its memory counts, its
  scheduled checks, files changed by its tasks, and screenshots taken on its
  desktop (`GET /api/agents/<id>/library`). A screenshot is served only to
  the agent whose desktop it was taken on.

## Parallel jobs (phase 5)

```dotenv
CONVEYOR_AGENT_PARALLEL_JOBS=2     # 1 (default) = one Codex job at a time
```

Above 1, every agent **with its own project folder** gets a lane
(`job_lanes.py`). Jobs in one lane never overlap; jobs in different lanes
may, up to the limit. Everything else — the default agent, unbound Telegram chats, Feishu,
agents without a folder — shares the `default` lane, because those jobs all
work in the configured workspace.

- The queue starts the oldest queued job whose lane is free
  (`queued_jobs.lane`). With the limit at 1 this is exactly the old rule:
  the oldest job, if nothing is running.
- Each lane has its own runner, so `/status`, `/cancel`, `/diff`, `/apply`
  and "the last job" in an agent's conversation mean that agent's jobs.
  Job records carry their lane; cleanup and audits still see all of them.
- Cancelling from the Web Console finds the job on whichever lane runs it.
- The limit is capped at 4. Two is a sensible ceiling on a 2-core host.
- Desktop tasks were already independent: one per desktop.

## Safety notes

- Instructions are operator-authored and shape the role, but the chat tier's
  numbered rules (no invented facts, write tools need confirmation, untrusted
  tool output) come after them in the prompt and still apply.
- List previews are redacted like any other text leaving the console.
