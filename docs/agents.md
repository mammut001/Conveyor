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
- Tasks from Telegram, Feishu, the default agent and the one-shot
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
- An agent without a project folder, and everything from Telegram and
  Feishu, keeps using the configured workspace.
- Host read-only tools (`/git_status`, file search) still look at the
  configured workspace.

## Safety notes

- Instructions are operator-authored and shape the role, but the chat tier's
  numbered rules (no invented facts, write tools need confirmation, untrusted
  tool output) come after them in the prompt and still apply.
- List previews are redacted like any other text leaving the console.
- All agents still share one memory and run one job at a time.
