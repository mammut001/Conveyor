# Conveyor Agent Supervisor

## Overview

The Conveyor **Agent Supervisor** coordinates multi-agent software engineering workflows organized around:

$$\text{Repository} \longrightarrow \text{Managed Worktree} \longrightarrow \text{Task}$$

The operator interacts in natural language. File diffs, worktree browsing, and terminal logs remain collapsed by default unless user action or failure review is required.

Development completion, pull request merge, and service deployment are tracked as separate, distinct states and are never conflated.

---

## Configuration & Environment Variables

The supervisor is controlled by the following environment variables. The feature flag is **off by default**.

| Environment Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `CONVEYOR_AGENT_SUPERVISOR_ENABLED` | bool | `false` | Master feature flag. Alias: `CONVEYOR_ANTIGRAVITY_SUPERVISOR_ENABLED`. |
| `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR` | string | `claude` | Default executor for analyst, implementer and reviewer (`pool`, `antigravity`, `claude`, `command-code`). `codex` is not implemented. |
| `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_EXECUTOR` | string | `None` | Role-specific override for implementer executor. |
| `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_MODEL` | string | `None` | Model identifier for implementer (e.g., `gemini-3.8-flash-medium`). |
| `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_LOCATION` | string | `worktree` | Execution working directory binding (`worktree` or `control_plane`). |
| `CONVEYOR_AGENT_SUPERVISOR_COORDINATOR_EXECUTOR` | string | `chat` | Coordinator executor (default: `chat` OpenAI-compatible client). |
| `CONVEYOR_AGENT_SUPERVISOR_COORDINATOR_MODEL` | string | `None` | Model override for coordinator. |
| `CONVEYOR_AGENT_SUPERVISOR_COORDINATOR_LOCATION` | string | `control_plane` | Execution binding for coordinator. |
| `CONVEYOR_AGENT_SUPERVISOR_ANALYST_EXECUTOR` | string | `None` | Analyst role executor override. |
| `CONVEYOR_AGENT_SUPERVISOR_ANALYST_MODEL` | string | `None` | Analyst model override. |
| `CONVEYOR_AGENT_SUPERVISOR_ANALYST_LOCATION` | string | `worktree` | Execution binding for analyst. |
| `CONVEYOR_AGENT_SUPERVISOR_REVIEWER_EXECUTOR` | string | `None` | Reviewer role executor override. |
| `CONVEYOR_AGENT_SUPERVISOR_REVIEWER_MODEL` | string | `None` | Reviewer model override. |
| `CONVEYOR_AGENT_SUPERVISOR_REVIEWER_LOCATION` | string | `worktree` | Execution binding for reviewer. |
| `CONVEYOR_AGENT_SUPERVISOR_MAX_REPAIR_ROUNDS` | int | `3` | Maximum repair iterations before failing (clamped 1–10). |
| `CONVEYOR_AGENT_SUPERVISOR_TASK_TIMEOUT_SECONDS` | int | `3600` | Timeout cap per role invocation in seconds (clamped 60–86400). Validation commands use `CONVEYOR_AGENT_SUPERVISOR_VALIDATION_TIMEOUT_SECONDS` per command; this is not a single end-to-end task deadline. |
| `CONVEYOR_ANTIGRAVITY_BIN` | string | `agy` | Path or command name for Antigravity CLI. Alias: `CONVEYOR_AGY_BIN`. |
| `CONVEYOR_AGENT_SUPERVISOR_VALIDATION_COMMANDS_JSON` | JSON | `""` | JSON list of host-executed verification command specs. |
| `CONVEYOR_AGENT_SUPERVISOR_VALIDATION_TIMEOUT_SECONDS` | int | `300` | Timeout per validation command in seconds (clamped 5–3600). |

---

## Generic 4 Roles & Responsibilities

The supervisor decouples model selection from role execution:

1. **Coordinator** (default executor: `chat`):
   - Single conversational entry point for the operator.
   - Evaluates goals against repository context.
   - Ordinary chat does NOT automatically dispatch coding tasks; dispatch requires explicit operator execution instructions.
   - Summarizes phase transitions and acceptance conclusions in clear operator-facing language.

2. **Analyst** (read-only):
   - Analyzes target codebase and constraints.
   - Outputs an implementation blueprint: file change scope and acceptance criteria.
   - Read-only execution is strictly enforced; analyst cannot take over code editing.

3. **Implementer** (write access):
   - Pluggable coding engine (`antigravity`, `claude`, `command-code`).
   - Receives analysis plan and acceptance criteria.
   - Applies modifications and runs internal checks within the assigned isolated git worktree.
   - Returns a plain-text change summary (not a structured verdict), executed commands, and the commit SHA.

4. **Reviewer** (read-only):
   - Independently checks candidate changes against acceptance criteria.
   - Binds review strictly to an explicit commit SHA (40 or 64 hex accepted by runtime) and 64-hex content fingerprint.
   - Evaluates host validation command outputs; only the reviewer verdict has a required JSON schema (`--json-schema`). Never accepts based solely on the implementer's self-report.

---

## Configuring Antigravity (AGY) as Implementer & Read Roles

Antigravity (`agy`) can be set directly as the implementer, without the pool (see the pool section below):

```bash
CONVEYOR_AGENT_SUPERVISOR_ENABLED=true
CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_EXECUTOR=antigravity
CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_MODEL=gemini-3.8-flash-medium
CONVEYOR_ANTIGRAVITY_BIN=agy
```

### AGY Security and Permission Controls

AGY read roles use a session-scoped permission deny profile; these tool permission rules are distinct from OS filesystem isolation and do not provide an OS sandbox for write roles:
- **Write roles (Implementer)**: Spawns `agy` with `--mode accept-edits` (does not have a proved OS sandbox).
- **Read-only roles (Analyst, Reviewer, Coordinator)**: Bypassing permissions (`--dangerously-skip-permissions`) is forbidden. Read roles pass session-only `--settings` containing an explicit tool permission deny profile:
  - Denies: `write_file(*)`, `command(*)`, `unsandboxed(*)`, `mcp(*)`, `execute_url(*)`, `read_url(*)`
  - Allows: `read_file(<canonical_worktree_path>)` with `allowNonWorkspaceAccess: false`.
- **Reviewer role**: In addition to read-only tool restrictions, passes `--json-schema` to enforce structured verdict parsing.

### Other Executors & Quota Notice

- **Claude (`claude`)**: Local CLI adapter with `--permission-mode` and tools allowlist.
- **Command-Code (`command-code`)**: Local CLI adapter with `--permission-mode` (`plan` for read, `accept-edits` for write).
- **Pool (`pool`)**: Picks one of the Claude Code, Antigravity or Command Code workers per role call and delegates to that worker's adapter. See the pool section below.
- **Codex (`codex`)**: Intentionally unsupported in this build; reports unavailable before dispatch.
- **Provider Quota Notice**: Executor `available()` checks verify local binary presence on the host. Upstream provider quota exhaustion, rate limits, or network failures are runtime execution errors, not availability guarantees.

---

## 执行者池 / pool executor

`pool` is a Supervisor executor that chooses a worker from the 执行者 pool (`workers.py`) for each role call. It does not run an agent of its own. It hands the call to the matching Supervisor adapter, so that adapter's read-only and sandbox rules still apply.

### Selecting it

- In the Code (项目) page, each analyst, implementer and reviewer role has an executor dropdown. The first entry, 执行者池（自动轮换）, is marked 推荐 when a pool worker is usable. The choice is sent with the dispatch request and applies to that task only.
- Only `analyst`, `implementer` and `reviewer` can be overridden from the browser. The coordinator stays on `chat`.
- In config, set `CONVEYOR_AGENT_SUPERVISOR_<ROLE>_EXECUTOR=pool` for one role, or `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR=pool` for analyst, implementer and reviewer together.
- Or create `supervisor_roles.json` in the Conveyor memory root (`codex_memory_root`, default `~/.codex`, set by `CODEX_MEMORY_ROOT`), keyed by role:

  ```json
  {"implementer": {"executor": "pool"}}
  ```

- Precedence in `load_roles`: the role's `CONVEYOR_AGENT_SUPERVISOR_<ROLE>_EXECUTOR` variable, then `supervisor_roles.json`, then the implementation executor default. A dispatch-time override beats all of them.

### Rules

- Candidates are pool workers that are enabled, installed, not `needs_login`, not cooling, and whose Supervisor adapter reports available. Mapping: Claude Code to `claude`, Antigravity to `antigravity`, Command Code to `command-code`. Pi, Super Grok and Codex never run through the pool.
- Read roles (analyst, reviewer) need a worker eligible for RUN. The implementer needs a worker eligible for FIX. Command Code is excluded from FIX unless `CONVEYOR_WORKERS_UNSANDBOXED=true`.
- Order follows the owner's drag order. Workers over their usage threshold (default 95%, set per worker from 1 to 100) are tried last. The threshold does not stop a run that has already started.
- Rotation happens only after a quota failure (quota wording, or a "resets in" reset time). The failed worker is cooled until the reported reset, or for 45 minutes when none is parsed. Any other failure ends the call without rotating.
- An implementer that has already changed its worktree is not re-run on another worker.
- Each attempt is a separate execution record with its own executor and model. The web console shows a switch note between a failed attempt and the next one.
- Each worker runs with its own saved model (`/api/workers/<id>/model`). The Supervisor role model is never passed to pool workers.
- Cooling is shared with the chat pool (`runner/pooled.py`). If every candidate hits a quota, the error starts with "执行者池里的执行者额度都用完了：".
- Usage readers live in `worker_usage.py`. Antigravity usage needs a running local Antigravity language server; without one, the pool relies on quota errors alone. See also the [worker pool notes](workers.md).
- `scripts/supervisor_pool_smoke.py` runs the real flow with fake `claude` and `agy` binaries and stubbed detection.

## Host Validation Commands (`CONVEYOR_AGENT_SUPERVISOR_VALIDATION_COMMANDS_JSON`)

Self-reported model checks are **never** proof of completion. Real acceptance requires host process verification.

Configure validation commands as a JSON array of objects or pairs:

```bash
export CONVEYOR_AGENT_SUPERVISOR_VALIDATION_COMMANDS_JSON='[
  {"name": "pytest", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]},
  {"name": "lint", "argv": ["flake8", "src"]}
]'
```

### Verification Rules
- Explicit `argv` lists only; arbitrary shell strings and browser-submitted arbitrary commands are rejected.
- Commands execute on the host directly in the target worktree and capture actual exit codes, stdout, stderr, and execution IDs.
- **Zero Validation Commands Rule**: If no operator validation commands are configured, the supervisor transitions the task to `waiting_user` with stage `needs_validation`. It **never** fabricates `completed`.

---

## Seeding the Agent Supervisor Skill

Conveyor stores skills in its internal `personal_tools.skills` SQLite database (`skills.db`), not Codex global skill paths.

### Generic Seeder CLI

```bash
# Preview parsing without writing to database
python3 scripts/seed_agent_supervisor_skill.py --dry-run

# Seed into isolated directory (useful for testing without touching user state)
python3 scripts/seed_agent_supervisor_skill.py --state-dir /tmp/isolated-state

# Normal runtime seed
python3 scripts/seed_agent_supervisor_skill.py
```

### Legacy Wrapper

The legacy wrapper forwards arguments directly to the generic seeder:

```bash
python3 scripts/seed_antigravity_supervisor_skill.py --state-dir /tmp/isolated-state
```

- **Idempotency**: Repeated executions update the existing `agent-supervisor` record without creating duplicates.
- **Preservation**: Custom and unrelated skills in `skills.db` are preserved; no automatic legacy data deletion occurs.

---

## Web API, 202 Dispatch & Cancellation UI

Supervisor actions are accessible over authenticated HTTP endpoints. Dispatch wakes a server-owned worker (`SupervisorWorkerManager` in `supervisor_service.py`). The worker claims queued tasks and runs `run_supervisor_flow`, at most 2 flows at a time:

- `POST /api/supervisor/tasks`: Creates a task in `pending_dispatch`.
- `POST /api/supervisor/tasks/<id>/dispatch`: Claims the task, acquires worktree lock, and enqueues supervisor execution. Returns `202 Accepted`:
  ```json
  {
    "status": "accepted",
    "task": {
      "id": "sup-task-001",
      "state": "dispatching",
      "worktree_path": "/path/to/worktree"
    }
  }
  ```
- `POST /api/supervisor/tasks/<id>/cancel`: Terminates in-memory process handles and process groups, releasing the worktree lock. Returns `200 OK`.
- `POST /api/supervisor/tasks/<id>/transition`: Direct browser task transitions are disabled (returns `409 Conflict`), ensuring state advances solely from real execution results.

---

## Deployment Truth: Store Tracking Only

The supervisor tracks deployment progress across 5 stages:

$$\text{local\_ci} \longrightarrow \text{tailscale\_transfer} \longrightarrow \text{vps\_verify} \longrightarrow \text{service\_switch} \longrightarrow \text{health\_check}$$

> [!IMPORTANT]
> In this build, deployment stages represent **database store tracking only** in SQLite.
> There is **no automated Tailscale network transport** or automated remote runner executed in this build.
> Never advertise automated deployment transport.

---

## Runtime Recovery & Cancellation Limits

- On daemon startup, `recover_on_startup(settings)` inspects active task records against running process PIDs on the host.
- **Stdio Reconnect Limitation**: Across daemon restarts, process handles can be checked for liveness, but stdio streams cannot reconnect. Tasks whose execution was interrupted are marked accordingly.
- Offline unit and smoke tests validate state machine contracts, database locks, and SHA binding invalidations; they do not constitute proof of real model behavior.

---

## Opt-In Live Model Integration Harness

To evaluate real Antigravity model execution end-to-end against live LLM providers, refer to the opt-in harness:

- Script: `scripts/agent_supervisor_live.py`
- Documentation: [agent-supervisor-live.md](agent-supervisor-live.md)

Run preflight (no model spend):
```bash
python3 scripts/agent_supervisor_live.py --root /tmp/isolated-preflight
```

Run live models with opt-in flag:
```bash
python3 scripts/agent_supervisor_live.py --run-real-models --root /tmp/isolated-live
```
