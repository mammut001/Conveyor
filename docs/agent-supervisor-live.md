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

---

## 目标闭环实测（`scripts/goal_live.py`）

用来验证「目标闭环」（`conveyor/agents/supervisor_goals.py`）在真实模型下能不能自己一轮一轮把事情做完。默认只做准备检查，不调用任何模型、不花额度。

**准备检查（默认）**：在 `--root` 里建一个小练习仓库（`calc.py` 的 `add` / `multiply` / `divide` 都还没写，`tests/test_calc.py` 目前不通过），登记仓库、新建托管工作目录（分支 `goal-calc`）、选中服务器检测到的 `unittest` 检查，先跑一次确认它确实不通过；再列出每个执行者能不能用、三个角色和评估器会用谁，以及实测时具体会做哪些事（`would_do`）。报告写到 `<root>/goal_live_preflight.json`，`ready_for_live` 为 `false` 时 `blockers` 里写着原因。

**实测（`--run-real-models`，明确同意才会花额度）**：同样建好仓库后，用 `supervisor_goals.create_goal` 创建一个最多 3 轮的目标，然后脚本自己按服务器后台 worker 的方式推进：领取任务（`agent_supervisor.claim_task` + `run_supervisor_flow`），再推进目标（`supervisor_goals.advance_goals`，同一时间只跑一个）。目标离开「进行中」（达成 / 停止 / 轮数用完 / 失败，或者进入「等你回复」——脚本不会替你回答），或者到了 `--timeout-minutes` 就停；超时会停止目标并取消还在跑的任务。最后打印 JSON 摘要（每轮的任务状态和评估结论、最终目标状态、改动的文件列表），并写到 `<root>/goal_live_result.json`。达成时退出码 0，否则 1；参数不对时 2。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--root` | 必填 | 空目录或还不存在的目录。不是空的、是文件、或者放在平时的数据目录（`CODEX_MEMORY_ROOT`，默认 `~/.codex`；`CODEX_TASK_ROOT`）或 Conveyor 代码目录里面，都会拒绝。 |
| `--run-real-models` | 关 | 明确同意调用真实模型。 |
| `--executor` | `auto` | 分析 / 实现 / 审阅用谁：`auto`、`pool`、`pi`、`claude`、`antigravity`、`command-code`。`auto` 按服务器的自动选择；其余值作为目标的角色设置发给每一轮任务。 |
| `--timeout-minutes` | `60` | 总时长，1–600 分钟。 |
| `--repair-rounds` | `1` | 每一轮任务里审阅不通过时最多再改几次，1–10。 |

隔离规则：工作区、任务和工作目录、Supervisor 数据库都放在 `--root` 下面；不读 `.env`、不继承运维配置的验证命令；目标通知（Telegram / 飞书 / 网页推送）关闭。评估器用的对话模型只从环境变量 `CONVEYOR_CHAT_API_KEY` / `CONVEYOR_CHAT_MODEL` / `CONVEYOR_CHAT_BASE_URL` 读取（「模型设置」里保存的配置在隔离目录里看不到）；执行者池和 Pi 保存过的设置同理。执行者 CLI（claude、agy 等）自己的登录和会话文件仍在它们平时的位置。准备检查用过的目录不能再用来实测，请换一个新的空目录。

```bash
# 准备检查（不花额度）
python3 scripts/goal_live.py --root /tmp/goal-live-check
# 实测
python3 scripts/goal_live.py --run-real-models --root /tmp/goal-live-run --executor claude --timeout-minutes 45
```

`tests/test_goal_live_script.py` 用假执行者和假评估器离线走完准备检查、参数校验、两轮达成和超时停止，不调用任何真实模型。
