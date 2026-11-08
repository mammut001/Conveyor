---
slug: antigravity-supervisor
name: Antigravity Supervisor
description: Coordinate chat, analysis, implementation and review roles across repositories and worktrees with persistent state, locks, acceptance and deployment tracking.
triggers: supervisor, antigravity, worktree, task, deploy, acceptance
---

# Antigravity Supervisor

> Legacy skill alias for Conveyor's reusable **Agent Supervisor**. See
> `agent-supervisor.md` for the primary generic definition.

## Product positioning

The operator talks in natural language. Code work is organized as
**repository → worktree → task**. File browsing, diffs and terminals are
optional technical details and stay collapsed by default. Run logs stay
collapsed unless a failure or user action is required.

## Roles

1. Coordinator (main chat model via OpenAI-compatible client, default executor `chat`)
   - Single conversational entry point.
   - Understands goals using current repository / worktree context.
   - Does not start development from ordinary discussion; creates and dispatches
     tasks only after explicit operator instruction to execute.
   - Summarizes stage results in operator-facing language.

2. Analyst (configurable model, e.g. Gemini / Claude)
   - Reads the relevant code and constraints before implementation.
   - Produces a concrete plan: impact scope, acceptance criteria.
   - Read-only by default; enforced strictly by runtime/CLI permission policy.
   - Never takes over file edits.

3. Implementer (Antigravity `agy` CLI adapter)
   - Receives the analysis plan and acceptance criteria.
   - Modifies code and runs checks inside the assigned isolated worktree (`--mode accept-edits`).
   - Returns plain-text change summary, commands actually run, results and commit SHA.
   - All project code changes are made by the implementer; analyst and reviewer
     roles are read-only and must not take over edits.

4. Reviewer (configurable model, read-only)
   - Independently checks the implementation against acceptance criteria.
   - Must target an explicit commit SHA (40 or 64 hex) and 64-hex content fingerprint.
   - Evaluates host-executed validation command outputs; only the reviewer verdict
     has a required JSON schema. Never accepts based only on the implementer's self-report.

Model names and executors are bound through configuration
(`CONVEYOR_AGENT_SUPERVISOR_*`), never hard-coded.

## Antigravity executor runtime profile

- **Write roles**: invoked with `--mode accept-edits` (distinct from an OS filesystem sandbox).
- **Read-only roles**: pass session-only `--settings` with an explicit permission deny profile
  blocking `write_file(*)`, `command(*)`, `unsandboxed(*)`, `mcp(*)`,
  `execute_url(*)`, and `read_url(*)`, allowing only `read_file(<worktree>)`
  with `allowNonWorkspaceAccess: false`.
- **Reviewer**: additionally passes `--json-schema` for structured verdict output.
- **Provider Quota Notice**: `available()` checks binary presence (`agy`); provider
  quota or network errors are execution errors, not availability guarantees.

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

Also distinguish: `failed`, `cancelled`, `waiting_user` (`needs_validation`), `interrupted`.

Development complete, PR merged and deployment successful are distinct states
and must never be conflated.

Every handoff contains:
- user goal and acceptance criteria
- repository, worktree, branch, code version (SHA and fingerprint)
- completed items and open issues
- evidence and artifact locations

On acceptance failure, provide concrete issues and reproduction steps before
handing back to the implementer. Default maximum 3 repair rounds; after that,
report remaining issues and transition to `failed` instead of looping.

If code changes after acceptance, the previous acceptance conclusion is
invalidated (`effective_acceptance` returns None) and re-verification is required.

## Runtime responsibilities (Conveyor)

Persistent state, task locks, execution processes and recovery are handled by
the Conveyor runtime (`agent_supervisor.py`):

- Prevent duplicate dispatch and conflicting writes on the same worktree.
- Timeout, cancel and process-exit detection.
- After restart, identify the actual execution status (stdio does not reconnect).
- Persist stage results, errors and acceptance evidence.
- State advances only from real execution results.

## Deployment flow (store tracking only)

```
local_ci → tailscale_transfer → vps_verify → service_switch → health_check
```

In this build, deployment tracking is strictly database store tracking in SQLite.
There is NO automated Tailscale network transport or remote deployment runner
implemented; do not advertise automated transfer.
