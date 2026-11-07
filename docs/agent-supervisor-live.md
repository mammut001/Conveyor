# Opt-In Real AGY Integration Harness (`scripts/agent_supervisor_live.py`)

## Overview

`scripts/agent_supervisor_live.py` provides an isolated, opt-in end-to-end integration harness executing real Antigravity (AGY) model processes across the analyst, implementer, and reviewer roles against the Conveyor agent supervisor runtime.

The script operates in a disposable standalone environment without altering production configuration, active git worktrees, or credentials.

## Key Architecture & Safety Controls

1. **Opt-In Guard (`--run-real-models`)**:
   - By default, the script executes in **preflight mode** (`--dry-run`), validating prerequisites (binary paths, git initialization, external validator self-test, and public lifecycle state transitions) without invoking real models or spending quota.
   - Passing `--run-real-models` explicitly engages the live AGY adapter pipeline.

2. **Standalone Disposable Workspaces & Strict Collision Refusal**:
   - Creates an isolated git repository (`clamp(value, lower, upper)` stub) in a dedicated root directory or `mkdtemp`.
   - If `--root` points to an existing non-empty directory, the script immediately aborts with a non-zero exit code (exit 1) to avoid clobbering existing fixtures, reports, or test data.
   - Spawns a supervised git worktree via `ags.create_worktree`.
   - Directs all task state, SQLite database tables, and evidence logs into an isolated temporary memory root (`codex_memory_root`).
   - Never performs automatic recursive cleanup on failure, preserving evidence for debugging.

3. **External Frozen Validator**:
   - Located outside the implementer worktree (`<root>/validators/validate_clamp.py`).
   - Validates comprehensive edge cases:
     - Normal range clamping (`clamp(5, 0, 10) == 5`)
     - Inclusive boundary handling (`clamp(0, 0, 10) == 0`, `clamp(10, 0, 10) == 10`)
     - Negative numbers and negative intervals (`clamp(-5, -10, -1) == -5`, `clamp(-15, -10, -1) == -10`)
     - Floating point bounds (`clamp(3.5, 1.0, 5.0) == 3.5`)
     - Out-of-range bounds (`clamp(-100, 0, 10) == 0`, `clamp(100, 0, 10) == 10`)
     - Invalid range detection (`lower > upper` raises `ValueError`)
   - Binds SHA-256 integrity verification before and after supervisor execution to ensure the test was not modified or weakened.
   - Invoked via host-configured operator validation command (`agent_supervisor_validation_commands`) using absolute `sys.executable -B` and worktree binding.

4. **Credential-Free, Bounded & Descendant-Audited Validator Execution**:
   - `run_frozen_validator_cmd` builds an allowlisted child environment with `subprocess_env.build_child_env(grants=())` ensuring zero provider credentials or secrets (e.g. OpenAI, Anthropic, Gemini, DeepSeek, Telegram) are inherited.
   - Sets `PYTHONDONTWRITEBYTECODE=1` in both child environment and process arguments (`sys.executable -B`).
   - Executes with an explicit deadline (`timeout_seconds`, default 30s) and actively caps combined stdout/stderr bytes (default 128 KB) using a non-blocking selector loop.
   - If a candidate hangs or floods output, the entire owned process group is killed and reaped immediately (`kill_process_group`), returning clear non-zero exit codes (124 on timeout, 125 on overflow).
   - Upon leader process exit, validates that no lingering background descendant processes survive in the process group (`_group_is_provably_dead`). If background descendants remain (e.g. background workers with closed stdio or processes holding inherited open pipes), the owned group is stopped and reaped within a bounded grace, returning non-zero exit code 126 to prevent falsely accepting programs that leave unverified background writers alive.

5. **AGY Executable Resolution**:
   - Supports bare command names (e.g., `--agy-bin agy`) resolved via `PATH`, environment variables (`CONVEYOR_ANTIGRAVITY_BIN`, `CONVEYOR_AGY_BIN`), or standard user fallbacks (`~/.local/bin/agy`).

6. **Controlled First-Round Fault Injection**:
   - Wraps `ags.get_executor` with `FaultInjectionProxyExecutor`, delegating `available()`, `cancel()`, and `run()` to the actual `AntigravityExecutor`.
   - For the **first implementer invocation only** (`attempt == 0`), appends an explicit instruction to commit an incomplete implementation (`def clamp(v, l, u): return v`).
   - Verifies that:
     1. The operator validation command fails.
     2. The supervisor records rejection (`passed=False`) in `supervisor_acceptance`.
     3. A repair round is initiated.
   - On the repair round (`attempt > 0`), the implementer receives the supervisor's repair prompt without fault injection, repairs the code, passes validation, and receives reviewer acceptance.
   - No fake execution results, simulated verdicts, or manual transitions are used; all outputs reflect genuine AGY adapter executions.

7. **Independent Verification & Explicit Invalidation Phases**:
   - Independently reruns the frozen validator against the final worktree under credential-free and bounded subprocess execution.
   - Validates `effective_acceptance(settings, task_id)` is passing.
   - An optional `--test-invalidation` flag tests post-completion tamper resistance: mutates a worktree sentinel file and verifies `effective_acceptance` immediately becomes invalidated (`None`).
   - If `--test-invalidation` is requested, any invalidation failure fails the overall workflow report (`status: "FAIL"`) and CLI exit (exit code 1).
   - Reports distinguish Phase 1 target acceptance (prior to intentional mutation) from Phase 2 invalidation testing, and explicitly note that the mutated candidate is not currently accepted.

## Limitations

- **Process-Level Isolation**: Independent validation enforces environment credential stripping, execution timeout, and output capping, but does not claim OS kernel/filesystem sandbox or container isolation.
- **Opt-In Real-Model Execution**: Real-model verification is strictly opt-in via `--run-real-models`; preflight mode is default to prevent unintended quota expenditure.

## CLI Options

| Flag | Default | Description |
| --- | --- | --- |
| `--run-real-models` | `False` | Explicit opt-in required to execute real AGY models. |
| `--analyst-model` | `gemini-3.8-flash-medium` | Model identifier for analyst role. |
| `--implementer-model` | `gemini-3.8-flash-medium` | Model identifier for implementer role. |
| `--reviewer-model` | `gemini-3.8-flash-medium` | Model identifier for reviewer role. |
| `--timeout` | `1800` | Task execution deadline in seconds. |
| `--repair-rounds` | `3` | Maximum allowed supervisor repair rounds. |
| `--root` | `""` | Directory for isolated workspace, tasks, and memory. Refuses non-empty directories. |
| `--agy-bin` | Auto-detected | Command name or path to `agy` CLI binary. |
| `--report-path` | `""` | Optional destination path for the JSON report (default: `<root>/agent-supervisor-live-report.json`). |
| `--test-invalidation`| `False` | Executes post-completion worktree mutation test. Failure causes overall run failure. |
| `--dry-run` | `False` | Forces preflight checks only. |

## Usage Examples

### Preflight / Dry-Run (No Model Spend)
```bash
python3 scripts/agent_supervisor_live.py --root /tmp/isolated-run-001
```

### Full Live Flow (Parent Execution)
```bash
python3 scripts/agent_supervisor_live.py \
  --run-real-models \
  --root /tmp/isolated-live-run-001 \
  --analyst-model gemini-3.8-flash-medium \
  --implementer-model gemini-3.8-flash-medium \
  --reviewer-model gemini-3.8-flash-medium \
  --test-invalidation
```
