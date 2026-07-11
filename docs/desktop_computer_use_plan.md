# P5.2 Computer Use — Implementation Plan

> **Status**: Historical P5.2 design document. The implementation described
> below is retained for design history only and is not the current contract.
> The shipped P5.6/P5.6.2 direct Computer Use stack is implemented through
> `desktop_computer_requests.py`, `desktop_computer_loop.py`, `desktop_cua.py`,
> and the local Mac agent. For current behavior and safety limits, read
> `docs/desktop_security.md` and `docs/desktop_capabilities.md`.

---

## 0. TL;DR for the operator

The original P5.2 design treated Computer Use as a deliberate stub. That is
historical context only: P5.6/P5.6.2 now provides an opt-in direct loop with
local Cua execution, an action allow-list, blocked-context guards, step/time
caps, redacted trajectories, and an operator kill switch. The sections below
describe the earlier confirmation-based design and should not be used as the
current runtime specification.

---

## 1. Where we are (exact current surface)

| Layer | File | State | What exists |
|---|---|---|---|
| Capability model | `nodes/types.py` | stub | `CAP_COMPUTER_USE_STUB` + `DESKTOP_STUB_CAPABILITIES`; full caps already defined (`CAP_MOUSE_CLICK` etc., L107–127). |
| Node registry | `nodes/registry.py` | stub | `build_stub_desktop_node()` returns OFFLINE; `is_stub_environment()` returns `True` (L232) — **the master switch**. |
| Control plane | `desktop_agent_server.py` | partial | Bearer-auth HTTP on `127.0.0.1:8766` (`ThreadingHTTPServer`). Has register/heartbeat/observe/upload/status. **No task/step endpoints.** |
| Local agent | `desktop_agent.py` | partial | `urllib`-based polling (`_control_plane_url()`, `_post_json` helper). `--poll-observe` + upload polling loops. **No task polling, no action executors.** |
| Menubar app | `menubar-agent/Sources/.../AgentSupervisor.swift` | implemented | Spawns `desktop_agent.py --poll-observe --poll-computer` as a TCC-attributed child. Needs **Accessibility** TCC for input synthesis. |
| Request store | `desktop_observe_requests.py` / `desktop_upload_requests.py` | DONE | **The template pattern** for tasks: cross-process file lock, `ALLOWED/RESULT_FORBIDDEN` fields, claim/complete/fail/cancel, TTL expiry. |
| Tool layer | `handlers/tools/registry.py`, `executors.py` | stub | `computer.status` registered but stubbed. `register_builtin_tools()` is the registration point. |
| Intent | `handlers/intent.py` | stub | `_COMPUTER_USE_PATTERNS` already routes to `computer.status`. Needs a real route target. |
| Confirmation | `handlers/tools/confirm.py`, `runner.py` | DONE | Token binding (`create_pending`/`get_pending`/`matches_context`) + Telegram/Feishu inline buttons. **Reused for steps.** |
| Audit | `handlers/tools/audit.py` | DONE | `audit/tools.log` JSONL with redaction. Computer Use needs a parallel `audit/desktop.log` (schema §2). |
| Config | `config.py` | partial | `conveyor_computer_use_default_mode` whitelisted to `observe_only`/`off` (L134, 385). Token in `SENSITIVE_FIELDS`. |

---

## 2. Design decisions (locked by the existing contracts)

1. **Direction of connection** — unchanged. The Mac agent connects *inbound*
   to the VPS control plane (`CONVEYOR_CONTROL_PLANE_URL`, default
   `http://127.0.0.1:8766`). No inbound firewall holes. (protocol §1)
2. **Who proposes steps** — the **VPS side** proposes; the **Mac executes
   only confirmed steps**. Matches `desktop.step.propose` (VPS→laptop) and
   `desktop.step.confirm` (VPS→laptop, operator approved). (protocol §2.4)
3. **Brain** — Codex on the VPS, in a *planner* role that emits structured
   step proposals (JSON), **not** raw shell. Each proposal is gated by
   `desktop.step.confirm`. Conveyor stays "the transport".
4. **Confirmation is two-layer** (security rule 5, 8; architecture §P5.1):
   the existing chat-level `confirm` token **AND** the `desktop.step.confirm`
   round trip are *both* required. A step with only one is rejected.
5. **Defaults** — `CONVEYOR_COMPUTER_USE_DEFAULT_MODE` gains a third value:
   `step_confirm` (per-step confirmed control). `observe_only` and `off`
   remain. A real button is only reachable when mode = `step_confirm` **and**
   the desktop node is ONLINE.
6. **Isolation** — screenshot bytes still never leave the Mac by default
   (P5.2/P5.3 rule). The planner works from `desktop.observe` metadata /
   region coordinates, not raw pixels on the VPS. (security §3)

---

## 3. New capability / config surface

### `nodes/types.py`
- `build_stub_desktop_node()` → `build_desktop_node(settings)` returning
  ONLINE-aware node whose capabilities = `DESKTOP_FULL_CAPABILITIES` when
  `conveyor_computer_use_default_mode == "step_confirm"` and agent online,
  else `DESKTOP_STUB_CAPABILITIES`.

### `nodes/registry.py`
- `is_stub_environment()` returns `False` once a real agent is wired
  **and** mode != `off`. This is the single flip that "turns on" the
  capability surface. Until then it stays `True` (safe default).
- `find_nodes_with_capability(CAP_COMPUTER_USE_STEP)` used by the task tool
  to resolve the target node from NL.

### `config.py` (new `Settings` fields)
| Field | Default | Notes |
|---|---|---|
| `conveyor_computer_use_default_mode` | `observe_only` | whitelist: `observe_only`/`step_confirm`/`off`. |
| `conveyor_computer_use_task_ttl_seconds` | `300` | 5-min blast-radius cap (security §4). |
| `conveyor_computer_use_step_ttl_seconds` | `120` | per-step confirm window. |
| `conveyor_computer_use_max_pending_tasks` | `1` | one interactive task at a time. |
| `conveyor_computer_use_denylist_windows` | `""` | comma list of window titles to refuse clicks on (password manager, banking). |
| `conveyor_computer_use_allow_domains` | `""` | per-task network allowlist (security §4 cross-domain). |

Keep `conveyor_desktop_agent_token` in `SENSITIVE_FIELDS` (already there).

---

## 4. New control-plane endpoints (`desktop_agent_server.py`)

Mirror the observe/upload endpoints' auth + validation (16 KB body cap,
`hmac.compare_digest`, 401 on bad token).

| Method & path | Body | Behaviour |
|---|---|---|
| `POST /desktop/task/create` | `{task_id, intent, payload, require_step_confirm}` | Validate mode != `off`; store pending task; reply `{ok, task_id}`. |
| `GET /desktop/task/pending?node_id=` | — | Return oldest pending/claimed task needing a step proposal or status. |
| `POST /desktop/task/claim` | `{task_id, node_id}` | Claim task (idempotent). |
| `POST /desktop/task/stop` | `{task_id, node_id, reason}` | Set `stopped`; reply `{ok, cleanup}`. |
| `POST /desktop/step/propose` | `{task_id, step_kind, args, screenshot_hash_before, model_intent}` | Append pending step to task; reply `{ok, step_id}`. |
| `GET /desktop/step/pending?task_id=` | — | Return next unconfirmed step for the operator to review. |
| `POST /desktop/step/confirm` | `{task_id, step_id, approved, note, confirmation_token}` | **Validate** the `confirmation_token` (matches the chat pending) **and** operator allowlist; if approved, mark step `confirmed` and signal the agent loop to execute; reply `{ok, queued}`. |
| `POST /desktop/step/complete` | `{task_id, step_id, node_id, result, screenshot_hash_after, error?}` | Mark step executed; append `audit/desktop.log` entry. |

The server remains **stateless across processes** — task state lives in a
new file-backed store (next section), exactly like `desktop_nodes.json`.

---

## 5. New request store — `desktop_task_requests.py`

Copy the *shape* of `desktop_observe_requests.py` (it is the cleanest
reference: `_load_unlocked`/`_save_unlocked` with `.tmp`+`os.replace`,
`file_lock`, TTL expiry, claim/complete/fail/cancel, `ALLOWED/RESULT_FORBIDDEN`
field validation). Differences:

- Record carries `task_id`, `node_id`, `intent`, `operator_id`,
  `chat_id`, `channel`, `require_step_confirm`, `steps: [ {step_id,
  step_kind, args, status, screenshot_hash_before/after, confirmation_token,
  model_intent, error?} ]`, `created_at`, `expires_at`, `status`
  (`pending`→`running`→`paused`/`stopped`/`completed`/`failed`/`expired`).
- `create_task_request()` enforces `max_pending_tasks` and that
  `is_desktop_online()` is true (reuse `nodes.state.is_desktop_online`).
- `append_step()` / `confirm_step()` / `complete_step()` mutate the step list
  under the same lock.
- `RESULT_FORBIDDEN_FIELDS` extended with anything that could carry pixel
  bytes or secrets (same policy as observe/upload).
- Heartbeat silence > `2 × heartbeat_interval` → task auto-`paused`, not
  cancelled (protocol §2.5).

---

## 6. Local agent action executors (`desktop_agent.py`)

New module `desktop_actions.py` (or functions in `desktop_agent.py`):

- `execute_click(x, y)` — `CGEvent` (via a tiny Swift helper or
  `CoreGraphics` through `pyobjc` if available; fallback: `cliclick`).
  **Pre-check**: coordinate inside main display bounds; window under the
  point is not in `conveyor_computer_use_denylist_windows`.
- `execute_type(text)` — `CGEvent` keyboard insertion.
- `execute_key_combo(keys)` — for ⌘C / ⌘V etc.
- `execute_browser_navigate(url)` — open `url` in default browser **only if**
  host ∈ task `allow_domains`; else refuse (security §4 cross-domain).
- `execute_step(step)` — `try` each; capture `screenshot_hash_after` via the
  existing `capture_screenshot_once`/`capture-screen-helper`; POST to
  `/desktop/step/complete`.

New polling loop `task_loop()` (run by `--poll-task`, or folded into
`--poll-observe` so the menubar app needs no change): poll
`/desktop/task/pending` → claim → run planner-requested steps that are
`confirmed` → complete. **Never executes a step whose status != `confirmed`.**

### Menubar / TCC
- `menubar-agent/Sources/ConveyorAgent/AgentSupervisor.swift` already spawns
  `--poll-observe`. Add `--poll-task` (or extend the flag) so the task loop
  runs in the same TCC-attributed child.
- Document the **Accessibility** entitlement requirement in
  `menubar-agent/README.md` (Screen Recording + Full Disk Access already
  listed; Accessibility is new for input synthesis). Add a Swift
  `AXIsProcessTrustedWithOptions` prompt in `PermissionHelper.swift`.

---

## 7. Tool layer + intent (VPS side)

### `handlers/tools/registry.py` — new `ToolSpec`s
| name | danger | executor |
|---|---|---|
| `computer.task.create` | `WRITE` | `exec_computer_task_create` (spawns planner Codex job → proposes steps) |
| `computer.task.status` | `READ` | `exec_computer_task_status` |
| `computer.task.stop` | `WRITE` | `exec_computer_task_stop` |
| `computer.step.confirm` | — | handled by confirm flow (no direct exec) |
| `computer.status` | `READ` | **replace stub** with real capability-aware status |

All `computer.*` WRITE tools go through `_request_confirmation` (reuse
`handlers/tools/runner.py::_request_confirmation` + inline buttons). On
confirm, they call `run_tool` → executor, which creates/updates the task store
**and** creates the `desktop.step.confirm` binding via
`handlers/tools/confirm.create_pending` so the operator's Yes/button carries
the `confirmation_token` that the server validates.

### `handlers/intent.py`
- Extend `_COMPUTER_USE_PATTERNS` so phrases like "在 Mac 上打开 Xcode" /
  "click the login button on my mac" / "type my password into …" route to
  `computer.task.create` **only when** `is_stub_environment()` is False and
  mode == `step_confirm`; otherwise they still fall to the honest
  `computer.status` stub. This keeps the "never silently enable" guarantee.

### `handlers/tools/runner.py::_invoke_tool`
- Add `elif tool_name == "computer.task.create": ...` branches mirroring the
  `desktop.observe.*` block, plus Feishu card renderers in
  `channel/feishu_cards.py` (`computer_task_card` with Confirm/Reject +
  step preview).

---

## 8. Audit (`audit/desktop.log`)

New writer `handlers/tools/desktop_audit.py` reusing `redact_text()` from
`handlers/tools/audit.py`. One entry per executed step with the fields from
`docs/desktop_security.md` §2: `ts, node_id, task_id, step_id, action_type,
model_intent, screenshot_hash_before, screenshot_hash_after,
confirmation_status, confirmation_token, operator_id, chat_id, channel,
result_preview, error_preview`. Git-ignored; same 1 MB rotation as
`tools.log`.

---

## 9. Blast-radius enforcement (code, not policy)

- **0 consecutive auto-steps**: the agent refuses any `confirmed != true`
  step. Enforced in `execute_step`.
- **5-min task cap**: `create_task_request` stamps `expires_at =
  now + conveyor_computer_use_task_ttl_seconds`; a running task past the cap
  is auto-`paused` and re-confirm required to continue.
- **Click region / denylist**: `execute_click` validates bounds + window
  title against `conveyor_computer_use_denylist_windows`.
- **Per-step network policy**: `execute_browser_navigate` checks host ∈
  task `allow_domains`.

---

## 10. Suggested sub-phase order (one thing at a time)

- **P5.2.0** — Task lifecycle plumbing: `desktop_task_requests.py`,
  server task/step endpoints (stub executors, no action), config fields.
  Smoke: store CRUD + server 401/auth.
- **P5.2.1** — Mac action executors + `task_loop()` + menubar TCC/Accessibility.
  Smoke: executors refuse denylist/out-of-bounds without network.
- **P5.2.2** — Two-layer confirmation wiring
  (`computer.task.create` → planner → `step.propose` → `step.confirm` →
  execute → `audit/desktop.log`). Blast-radius caps in code.
- **P5.2.3** — Intent + tools + Feishu cards; flip `is_stub_environment()`
  to False when mode == `step_confirm`; capability swap; replace
  `computer.status` stub with real status.
- **P5.2.4** — Hardening + `scripts/computer_use_smoke.py` (mirror
  `scripts/nodes_smoke.py`): registry, intent, executor, card builder,
  confirm context-binding, audit schema, denylist, TTL. Add to `make smoke`.
- Update `docs/desktop_agent_protocol.md` (mark §2.2/§2.4 implemented) and
  `docs/desktop_security.md` (note what is now enforced in code), and
  `architecture.en.md` phase table.

---

## 11. Open questions to decide before coding

1. Planner role: reuse `handle_codex_job` with a constrained prompt that
   emits JSON steps, or a dedicated `JobMode.COMPUTER_USE_PLANNER`?
2. Step previews to the operator: send the *before* screenshot thumbnail
   (P5.4 upload path) with each Confirm button? (Privacy: thumbnail only,
   low-res, same as P5.4.)
3. Multi-desktop routing: NL needs a target-node hint when >1 desktop online
   (protocol §5 open question). Defer to P5.2.x+.

---

## 12. Conventions reminder (from `project.md`)

- Commit style `<area>: <one-line>` (e.g. `desktop: add computer.task store`).
- "Smoke first, deploy second"; never `rsync --delete`; one thing at a time.
- All new settings fall back safely; a typo in mode → `observe_only`.
