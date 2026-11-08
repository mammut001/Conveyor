---
slug: agent-supervisor
name: Agent Supervisor
description: Coordinate coordinator, analyst, implementer and reviewer roles across repositories and worktrees with persistent state, locks, acceptance and deployment tracking.
triggers: supervisor, agent, worktree, task, deploy, acceptance
---

# Agent Supervisor

## Product positioning

The operator talks in natural language. Code work is organized as
**repository → worktree → task**. File browsing, diffs and terminals are
optional technical details and stay collapsed by default. Run logs stay
collapsed unless a failure or user action is required.

## Roles

1. Coordinator (main chat model via OpenAI-compatible endpoint, default executor `chat`)
   - Single conversational entry point.
   - Understands goals using current repository / worktree context.
   - Does not start development from ordinary discussion; creates and dispatches
     tasks only after explicit operator instruction to execute.
   - Summarizes stage results in operator-facing language.

2. Analyst (configurable model, default executor matches implementation executor)
   - Reads the relevant code and constraints before implementation.
   - Produces a concrete plan: impact scope and acceptance criteria.
   - Read-only by default; enforced strictly by runtime/CLI permission policy.
   - Never takes over file edits.

3. Implementer (pluggable executor: Antigravity, Claude Code, Command-Code, or the pool `pool`)
   - Receives the analysis plan and acceptance criteria.
   - Modifies code and runs checks inside the assigned isolated worktree (`accept-edits` mode).
   - Returns plain-text change summary, commands actually run, results and the commit SHA.
   - All project code changes are made by the implementer; analyst and reviewer
     roles are read-only and must not edit project files.

4. Reviewer (configurable model, read-only)
   - Independently checks the implementation against acceptance criteria.
   - Must target an explicit commit SHA (40 or 64 hex) and 64-hex content fingerprint.
   - Evaluates host-executed validation command outputs; only the reviewer verdict
     has a required JSON schema. Never accepts based only on the implementer's self-report.

Model names and executors are bound through configuration
(`CONVEYOR_AGENT_SUPERVISOR_*`), never hard-coded.

## Executor status & protocol

Executors implement the protocol:
- `available() -> tuple[bool, str]`: declares capability and readiness.
- `run(...) -> ExecutionResult`: executes role prompt in worktree within timeout.
- `cancel(...) -> bool`: terminates active execution process.

Implementer and role choices in this build:
- **Antigravity (`antigravity` / `agy`)**: fully implemented CLI executor adapter.
  Write roles run with `--mode accept-edits` (distinct from an OS filesystem sandbox).
  Read-only roles pass session-only `--settings` with an explicit permission deny profile
  blocking `write_file(*)`, `command(*)`, `unsandboxed(*)`, `mcp(*)`, `execute_url(*)`,
  and `read_url(*)`, allowing only `read_file(<worktree>)` with `allowNonWorkspaceAccess: false`.
  The reviewer role additionally passes `--json-schema` for structured verdict output.
- **Claude (`claude`)**: implemented local CLI adapter. Read-only roles are
  enforced by CLI tool allowlist and `permission-mode: plan`.
- **Command-Code (`command-code`)**: implemented local CLI adapter. Read-only
  roles use plan mode; write roles use accept-edits.
- **Provider Quota Notice**: For CLI adapters, `available()` verifies local binary
  presence. Account/provider rate limits, quota exhaustion, or network failures
  are runtime execution errors, not availability guarantees.
- **Pool (`pool`)**: chooses one worker from the 执行者 pool per role call and
  delegates to that worker's adapter. See the pool section below.
- **Codex (`codex`)**: adapter is unsupported / NOT wired in this build; reports
  unavailable with an explicit diagnostic message if selected.

## 执行者池 / pool executor

`pool` picks a worker from the 执行者 pool (`workers.py`) for each role call. It
does not run an agent itself; the matching Supervisor adapter runs the call, so
that adapter's read-only and sandbox rules still apply.

Selecting it:
- In the Code (项目) page, each analyst, implementer and reviewer role has an
  executor dropdown. 执行者池（自动轮换） is the first entry. The choice applies to
  that task only.
- Config: `CONVEYOR_AGENT_SUPERVISOR_<ROLE>_EXECUTOR=pool`, or
  `CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR=pool` for all three roles.
- Or `supervisor_roles.json` in the Conveyor memory root (`codex_memory_root`,
  default `~/.codex`): `{"implementer": {"executor": "pool"}}`.
- Precedence: role env var, then `supervisor_roles.json`, then the default. A
  dispatch-time override beats all of them. The coordinator stays on `chat`.

Rules:
- Candidates are workers that are enabled, installed, not `needs_login`, not
  cooling, and whose adapter reports available. Mapping: Claude Code to `claude`,
  Antigravity to `antigravity`, Command Code to `command-code`. Pi, Super Grok and Codex never run
  through the pool.
- Analyst and reviewer need RUN eligibility. The implementer needs FIX
  eligibility; Command Code is excluded from FIX unless
  `CONVEYOR_WORKERS_UNSANDBOXED=true`.
- Owner's drag order first. Workers over their usage threshold (default 95%) are
  tried last. The threshold does not stop a run already in progress.
- Rotate only after a quota failure (quota wording or a "resets in" time). The
  failed worker cools down until the reported reset, or 45 minutes if none is
  parsed. Any other failure ends the call without rotating.
- An implementer that already changed its worktree is not re-run elsewhere.
- Each attempt is its own execution record with its own executor and model. The
  web console shows a switch note between a failed attempt and the next one.
- Pool workers use their own saved model, never the Supervisor role model.
- Cooling is shared with the chat pool. If all candidates hit a quota, the error
  starts with "执行者池里的执行者额度都用完了：".

## Verification & host validation commands

Runtime status and recovery track process handles and PIDs across daemon
restarts. Note that stdio streams cannot reconnect across daemon restarts.

## Verification & host validation commands

- Model self-reported checks are NOT proof of correctness.
- Acceptance requires real process evidence captured from host-configured
  operator validation commands (`CONVEYOR_AGENT_SUPERVISOR_VALIDATION_COMMANDS_JSON`).
  These run directly on the host using explicit argv lists (never browser
  commands or arbitrary shell strings).
- If no operator validation commands are configured, the supervisor transitions
  to `waiting_user` with stage `needs_validation`. It NEVER fabricates completion.

## Supervisor flow

Task states:

```
pending_dispatch → dispatching → analyzing → implementing → reviewing → completed
```

On acceptance failure:

```
reviewing → repair_pending → implementing → reviewing
```

Additional distinct states:
- `waiting_user` (stage `needs_validation` when validation commands are unconfigured)
- `failed` (exceeded repair cap or unrecoverable error)
- `cancelled` (operator requested cancellation)
- `interrupted` (process terminated externally)

Development complete, PR merged and deployment successful are distinct states
and must never be conflated.

Every handoff contains:
- user goal and acceptance criteria
- repository, worktree, branch, code version (commit SHA and content fingerprint)
- completed items and open issues
- evidence and artifact locations

On acceptance failure, provide concrete issues and reproduction steps before
handing back to the implementer. Default maximum 3 repair rounds; after that,
report remaining issues and transition to `failed` instead of looping indefinitely.

If code changes after acceptance, the previous acceptance conclusion is
invalidated (`effective_acceptance` returns None) and re-verification is required.

## Runtime responsibilities (Conveyor)

Persistent state, task locks, execution processes and recovery are handled by
the Conveyor runtime (`agent_supervisor.py`):

- Prevent duplicate dispatch and conflicting writes on the same worktree.
- Process cancellation via process-group termination and PID cleanup.
- Startup recovery: inspect running task state on daemon restart.
- Persist stage results, errors, handoffs and acceptance evidence.
- State advances only from real execution results.
- Follow existing authorization rules; do not repeatedly ask for confirmation.

## Deployment flow (store tracking only)

```
local_ci → tailscale_transfer → vps_verify → service_switch → health_check
```

In this build, deployment tracking is strictly database store tracking in SQLite.
There is NO automated Tailscale network transport or remote deployment runner
implemented; do not advertise automated transfer. Stages record source machine,
target machine, build version, live version, and failed stage details as updated
by external deployment scripts.
