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

1. Coordinator (current main chat model, e.g. Step5)
   - Single conversational entry point.
   - Understands goals using current repository / worktree context.
   - Does not start development from ordinary discussion.
   - Creates and dispatches tasks only after the operator asks to execute.
   - Summarizes stage results in operator-facing language.

2. Analyst (configurable model, e.g. Opus)
   - Reads the relevant code and constraints before implementation.
   - Produces a concrete plan: impact scope, acceptance criteria.
   - After implementation, independently (as Reviewer) inspects the diff and runs validation.
   - Acceptance must target an explicit commit SHA or identifiable workspace snapshot.
   - Never accepts based only on the implementer's self-report.

3. Implementer (pluggable executor: Antigravity, Codex, Claude Code, ...)
   - Receives the analysis plan and acceptance criteria.
   - Modifies code and runs checks inside the assigned isolated worktree.
   - Returns change summary, commands actually run, results and the code version.
   - All project code changes are made by the implementer; analyst and reviewer roles
     are read-only by default and must not take over edits.

4. Reviewer (configurable model, read-only)
   - Independently checks the implementation against acceptance criteria.
   - Must target a specific SHA/snapshot.

Model names and executors are bound through configuration
(`CONVEYOR_AGENT_SUPERVISOR_*`), never hard-coded. Unimplemented executors
(Antigravity in this build) report unavailable instead of silently falling back.

## Supervisor flow

Task states:

pending_dispatch → analyzing → implementing → reviewing → completed

On acceptance failure:

reviewing → repair_pending → implementing → reviewing

Also distinguish: failed, cancelled, waiting_user, interrupted.

Development complete, PR merged and deployment successful are distinct states
and must never be conflated.

Every handoff contains:
- user goal and acceptance criteria
- repository, worktree, branch, code version
- completed items and open issues
- evidence and artifact locations

On acceptance failure, provide concrete issues and reproduction steps before
handing back to the implementer. Default maximum 3 repair rounds; after that,
report remaining issues instead of looping.

If code changes after acceptance, the previous acceptance conclusion is
invalid and re-verification is required.

## Runtime responsibilities (Conveyor)

Persistent state, task locks, execution processes and recovery are handled by
the Conveyor runtime (`agent_supervisor.py`):

- Prevent duplicate dispatch and conflicting writes on the same worktree.
- Timeout, cancel and process-exit detection.
- After restart, identify the actual execution status; do not re-launch tasks
  that may still be running.
- Persist stage results, errors and acceptance evidence.
- State advances only from real execution results.
- Follow existing authorization rules; do not repeatedly ask for confirmation.

## Executor interface

Executors must declare capabilities (available/unavailable with reason),
support start, status, cancel, reconnect/recover, and result/evidence
collection. Not all executors support the same capabilities; unavailable
executors are reported explicitly and never silently substituted.

## Deployment flow (tracked separately)

local_ci → tailscale_transfer → vps_verify → service_switch → health_check

Represent with device icons, connectors and stage status. Tailscale is the
network link; build and tests run on the execution node. Show source machine,
target machine, build version, live version and the failed stage. Only mark a
stage successful after it actually ran and was verified; never simulate
progress.
