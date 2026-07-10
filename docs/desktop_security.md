# Desktop Security Contract — Phase 0 Stub

> **Status**: Design stub. No real desktop control is implemented in this task.
> **Audience**: anyone wiring a real local desktop agent in a future task, plus reviewers auditing the phase-0 stub.

This document locks down the **safety contract** for the future
local desktop agent. The phase-0 task ships only the registry
shape and the deterministic `computer.status` stub, but the
rules below are the bar a future implementation must clear
before any operator sees a working button.

---

## 1. Hard rules (cannot be relaxed without an explicit phase)

These rules are non-negotiable for the project; if a future
feature wants to relax any of them, it has to be a separate,
audited change that re-validates this document.

1. **VPS tasks may run Codex and server tools** (existing
   `danger-full-access` sandbox, allowlist, audit).
2. **Desktop tasks are not executed** unless a local desktop
   agent is **online** and **explicitly approved** by the
   operator. The phase-0 stub is offline regardless of env vars.
3. **Desktop control defaults to `observe_only`**. The
   `CONVEYOR_COMPUTER_USE_DEFAULT_MODE` env var is whitelisted
   to `observe_only` and `off` — a typo falls back to
   `observe_only` with a logged warning.
4. **P5.2/P5.3 add read-only screenshot observe only** via
   `capture-screen-helper`. Local: `desktop_agent.py --observe-once`.
   Remote (P5.3): chat creates observe requests; Mac agent polls
   with `--poll-observe` and returns **metadata only** — no image
   bytes cross the network. Screenshots stay on the Mac by default;
   no upload, no OCR, no LLM visual analysis. **Click, typing,
   browser control, password entry, payment action, file deletion,
   and form submission remain unimplemented.** See
   `docs/desktop_screenshot_observe.md`.
5. **Future desktop actions must require step-by-step
   confirmation by default.** The current chat-level confirmation
   token is the minimum; the agent must additionally refuse to
   execute a step that has not been confirmed in the
   `desktop.step.confirm` round trip.
6. **The agent is always treated as untrusted for state-changing
   actions.** The shared secret only authenticates the channel;
   it does not authorise an action.
7. **Operator allowlist gating applies to every callback**, not
   just typed messages. The Feishu card action handler already
   routes card presses through `is_allowed`; the future
   desktop-step-confirm path must do the same.
8. **Operator + chat + channel binding is required for every
   task.** The same `matches_context` check used by
   `handlers.tools.confirm` is reused so a different chat
   cannot resume a task started in another.
9. **Computer Use is never automatic by default.** A
   natural-language "open Xcode on my Mac" message must never
   execute a step without the operator pressing a confirm button
   (or sending the confirmation text). Codex must not "decide"
   to call the agent. The one exception is the explicit
   opt-in **direct mode** (§7): only after `/computer_arm` (TTL)
   or `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` does the loop run
   hands-free — and even then every step is still gated by the
   §7.2 safety envelope (action allow-list, blocked-keyword
   guard, redaction, caps, kill switch).

---

## 2. Audit log requirements

When real desktop control lands, every step the agent executes
must append an entry to `audit/desktop.log` (parallel to the
existing `audit/tools.log`) with the following fields at
minimum:

| Field | Why |
|---|---|
| `ts` | Wall-clock UTC timestamp. |
| `node_id` | Which desktop node the step ran on. |
| `task_id` | The local task identifier; ties multiple steps together. |
| `step_id` | Per-step counter so a single task has an ordered history. |
| `action_type` | e.g. `screenshot`, `mouse.click`, `keyboard.type`, `browser.navigate`, `paste`, `submit_form`, `payment`. |
| `model_intent` | The natural-language intent Codex produced to justify the step. Truncated + redacted. |
| `screenshot_hash_before` | SHA-256 of the screenshot taken immediately before the step. |
| `screenshot_hash_after` | SHA-256 of the screenshot taken immediately after the step. |
| `confirmation_status` | One of `confirmed`, `auto_confirmed` (for a tiny observe-only allowlist), `rejected`, `timed_out`. |
| `confirmation_token` | The token from `handlers.tools.confirm` that approved the step. |
| `operator_id`, `chat_id`, `channel` | Same triple as the typed message that started the task. |
| `result_preview` | First ~200 chars of the agent's reply, redacted. |
| `error_preview` | Truncated exception type + message, if the step failed. |

The audit writer must apply the same redaction policy as
`handlers/tools/audit.py` (no tokens, no passwords, no .env
content). The log file is git-ignored.

---

## 3. Sensitive content

The agent must never exfiltrate:

- Operator passwords or session cookies.
- Payment instrument data, including card numbers, CVV, full
  bank account numbers.
- Personal health, government ID, or other PII unless the
  operator explicitly opted in for that task.
- The contents of password manager windows.
- Any file the operator has not explicitly named in the task.

Screenshots are a special case: in phase 0 the protocol allows
the agent to send screenshot bytes to the VPS only for the
purpose of producing a `desktop.observe` summary, and only when
the operator requested it. A future tightening may require
screenshot bytes to stay on the laptop and only structured
data (text + bounding boxes) ever crosses the wire.

---

## 4. Blast-radius limits

The future agent must implement the following caps in code,
not just in policy:

- **Max consecutive auto-steps**: 0 (every step is confirmed).
- **Max time per task**: 5 minutes wall-clock; the agent must
  pause and re-confirm past that limit.
- **Max region of interest per click**: the agent must refuse a
  click that lands outside the visible screen or on a window
  whose title matches a deny-list (e.g. password manager,
  banking app).
- **Per-step network policy**: the agent must refuse a step
  whose action would send data to a domain not in the task
  payload. Cross-domain navigation is denied by default.

---

## 5. What the phase-0 stub actually enforces today

Today the registry is honest: a `nodes.status` call returns
the desktop node as `offline` regardless of configuration.
The `computer.status` tool returns text that explicitly says:

- "no real screenshot / mouse / keyboard / browser control is
  implemented yet",
- "next step is to run a local desktop agent",
- "no desktop action was performed".

The natural-language router sends "帮我在 Mac 上打开 Xcode"
and similar phrases to `computer.status` instead of Codex, so
the operator gets a deterministic answer rather than Codex
hallucinating a screen the VPS cannot see.

The card UI is read-only refresh; there is no "execute" button
on the node status card. This is intentional: a future task
that wants to add a real execute button must also update this
document and re-validate the audit + blast-radius sections.

---

## 6. P5.1 Desktop agent heartbeat Security

In P5.1 and P5.1.1, the desktop agent registration and heartbeat endpoint has been secured:
* **Token Authentication**: The shared secret `CONVEYOR_DESKTOP_AGENT_TOKEN` is required to be passed as a Bearer token in the `Authorization` header. If it's missing or invalid, the control plane rejects it with `401 Unauthorized`.
* **Constant-Time Token Comparison**: Direct string equality check for tokens was replaced with `hmac.compare_digest` to prevent timing attacks.
* **Request Hardening & Payload Size Enforcement**: Overly large payload requests are rejected immediately; bodies are capped at 16 KB and unbounded body streams are not read.
* **Input Sanitization and Validation**: Strict validations are performed on registration and heartbeat fields (e.g. max key sizes, expected `node_id` matching, host parameter keys platform/hostname/arch parsing), preventing arbitrary JSON bloat.
* **Token Redaction**: The shared secret token is listed under `SENSITIVE_FIELDS` in `config.py` and is never printed in server logs or application runtime logs.
* **Control Plane Binding**: By default, the control plane server binds to localhost `127.0.0.1`. Binding to `0.0.0.0` is disabled by default. Exposing this interface publicly requires secure routing (e.g. Tailscale, Cloudflare Tunnel, or a VPN) and HTTPS.
* **File-backed Persistence Isolation**: Status info is shared across processes using `CODEX_MEMORY_ROOT/state/desktop_nodes.json`. This JSON state file is guaranteed to contain NO tokens, secrets, or request headers, and only stores general connection metadata (node ID, version, host summary). No screenshots, mouse, keyboard, or action data are written or stored.
* **P5.2 observe**: Read-only screenshot capture is local-only through `capture-screen-helper`. Metadata JSON is stored under `CODEX_MEMORY_ROOT/desktop/screenshots/`; image bytes are not sent to the VPS.
* **P5.3.1 request store hardening**: The observe request store is hardened with a cross-process file lock. This prevents lost updates when Telegram, Feishu, and `desktop_agent_server.py` read/write `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json` concurrently.
* **Safety Non-goals**: No mouse/keyboard control, browser automation, remote screenshot trigger, or Gemini Computer Use API calls are active by default. Computer Use control is implemented in P5.6 behind the gated **direct mode** (see §7); it is **OFF by default** and never exposes the Cua driver to the network.

---

## 7. P5.6 Direct Computer Use Mode (cua backend)

P5.6 adds a hands-free direct computer-use mode: `Telegram/Feishu NL → Codex → Conveyor computer-use tools → Mac desktop_agent → local cua-driver → real desktop actions`. The backend is `trycua/cua`, run **only on the Mac desktop agent side**. The VPS never speaks the Cua protocol; it only writes step requests to a file store that the Mac agent polls.

### 7.1 Off-by-default, opt-in

- Every flag defaults to `false`/`disabled`:
  `CONVEYOR_COMPUTER_USE_ENABLED=false`,
  `CONVEYOR_COMPUTER_DIRECT_ENABLED=false`,
  `CONVEYOR_COMPUTER_ALWAYS_DIRECT=false`.
- Layered gates:
  - `CONVEYOR_COMPUTER_USE_ENABLED=true` unlocks status and read-only observe readiness.
  - `CONVEYOR_COMPUTER_DIRECT_ENABLED=true` is **also required** for `/computer_arm`,
    `/computer_task`, `/computer_action`, and for `is_direct_mode_active` to return true.
- Direct (hands-free, no per-step confirmation) mode is reached only one of two ways
  (both require USE + DIRECT enabled):
  1. **Arm TTL** — `/computer_arm [minutes]` arms direct mode for a limited time
     (`is_direct_mode_active` = USE AND DIRECT AND (`CONVEYOR_COMPUTER_ALWAYS_DIRECT`
     OR a non-expired arm)). After expiry the task is blocked.
  2. **Always-direct** — `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` bypasses arming,
     but only when both `CONVEYOR_COMPUTER_USE_ENABLED=true` and
     `CONVEYOR_COMPUTER_DIRECT_ENABLED=true`. ALWAYS_DIRECT alone never enables
     direct mode if DIRECT is off.
- `/computer_task <goal>` / `/computer_action` fail fast if direct mode is not active.
- `/computer_task` creates its persistent task record before acknowledging the
  chat and runs in a channel-local background task; `/computer_stop` can be
  handled while a long-running task is waiting on Cua.
- Slash aliases: `/computer_observe` (same as `/computer_screenshot` / `computer.observe`),
  `/computer_action <json>` → `computer.action`.
- Node capability gating: `computer_use_active(settings)` must be true; otherwise the
  `computer.use.direct` capability is not advertised.

### 7.2 Hard safety envelope (applies even in direct mode)

The relaxation of per-step confirmation is **not** a relaxation of safety. Regardless
of armed/always-direct, every step is still gated by:

1. **Action allow-list** — only `observe`, `click`, `type`, `hotkey`, `scroll`,
   `wait`, `done`, `stop` are accepted (`is_action_allowed` +
   `normalize_action`). Anything else is rejected before execution.
2. **Blocked-keyword guard** — if the goal or any step's context contains
   `password`, `passcode`, `bank`, `payment`, `crypto`, `keychain`,
   `system settings`, `delete account`, the task **stops and reports**; no desktop
   action is taken. Operators may add words with `CONVEYOR_COMPUTER_BLOCKED_KEYWORDS`,
   but cannot remove these built-in hard blocks.
3. **No secret injection** — the loop never injects values from env/memory into
   typed text. Typed text is limited to the operator-provided goal / explicit action
   payload.
4. **Redaction** — typed text and hotkey payloads are never stored raw.
   `redact_computer_action` strips `text`/`keys` to length + redaction marker in the
   trajectory and audit copy.
5. **Result allow-list** — only allow-listed fields from the driver
   (e.g. `png_bytes`, `ocr`, `window_title`, `text`) are accepted; forbidden fields
   (`password`, `token`, …) are dropped by the driver's `_allow_list`.
6. **Blast-radius caps** — `CONVEYOR_COMPUTER_MAX_STEPS` (default `20`) and
   `CONVEYOR_COMPUTER_MAX_SECONDS` (default `600`) bound any single task; the loop
   stops at either limit.
7. **Kill switch** — `/computer_stop` cancels the active task immediately
   (`cancel_computer_task`). Setting `CONVEYOR_COMPUTER_USE_ENABLED=false` (the
   default) or `CONVEYOR_COMPUTER_DIRECT_ENABLED=false` disables it entirely;
   changing config requires a restart to re-read.

### 7.3 Cua stays local

- `CONVEYOR_CUA_DRIVER_CMD` (default `cua-driver mcp`) shells out **only on the Mac
  agent**. The configured command is used to locate the local `cua-driver` binary;
  execution goes through the driver's local CLI wrapper (`cua-driver call <tool>
  <json>`) rather than exposing the MCP stdio server on a socket or network port.
  `probe_cua_driver` returns metadata only (`available`/`path`/`version`/permission
  status/`error`) and never runs a real desktop action during the probe.
- On macOS, real observe/click/type requires Cua's TCC permissions. Operators should
  run `cua-driver permissions grant` on the Mac agent and verify with
  `cua-driver permissions status --json`; `/computer_status` surfaces that
  metadata-only permission state. `scripts/cua_driver_real_smoke.py` is a
  read-only local verifier for the installed/authorized Mac agent path.
- The VPS `HttpComputerBackend` only polls the shared file store
  (`CODEX_MEMORY_ROOT/state/desktop_computer_requests.json`) for step completion; it
  never opens a Cua/automation connection. No Cua traffic crosses the network.
- The Mac agent executes steps in `desktop_agent.py --poll-computer`, calling
  `build_driver` → `CuaDriver.execute`, then reports the (allow-listed, redacted)
  result back to the control plane.

### 7.4 Audit

Each executed step appends a redacted trajectory entry
(`timestamp`, `screenshot_id`/`hash`, `action_type`, redacted args, `result`) to the
task record in the file store. Typed text and hotkeys are redacted in every stored
copy. The trajectory is visible via `/computer_log [task_id]`.
After every executed action other than `done`/`stop`, the loop performs a
follow-up `observe` before asking the planner for its next action. This keeps
planner state tied to the post-action desktop and distinguishes click success
from verified UI state. Only short allow-listed metadata such as `active_app`
and `click_method` is retained; raw screenshots and UI text are not.

### 7.5 P5.6.1 Hardening Upgrades

P5.6.1 hardens the computer-use implementation for safer and more debuggable hands-free operation:
1. **AX-First Click Preference**: Click actions containing AX metadata (`pid`/`window_id`/`element_index`) will prioritize AX-based clicks first. XY-coordinate clicking is used only as a fallback, and the click method used (`ax_click` vs `xy_click`) is recorded.
2. **App Allowlist/Blocklist Validation**: Prevents actions when the target/frontmost application matches a blocked app (defaults to `Keychain Access,System Settings,Terminal`) or, if `CONVEYOR_COMPUTER_ALLOWED_APPS` is specified, when a **mutating** action's target app is not in the allowlist. AX actions with `pid` resolve the **target** process name (not merely frontmost). Read-only `observe`/`wait` apply **blocklist only** so a Calculator-only allowlist does not reject the planner's first observe while Codex is frontmost.
3. **Observe → AX hints for the planner**: Successful `observe` best-effort attaches `pid` / `window_id` / `ax_app` / short `element_hints` (button labels only, truncated) so CodexPlanner can emit AX clicks instead of bare x/y. Hints prefer allowed-apps windows when the allowlist is set.
3. **Structured JSONL Trajectories**: Logs all steps to `codex_memory_root/computer/trajectories/<task_id>.jsonl` with timestamp, task ID, step index, screenshot ID/hash, action type, redacted args, result status, error, and step duration (duration_ms). Directory tree `computer/` and `computer/trajectories/` are chmod `0700` when possible; each JSONL file is chmod `0600`.
4. **Concise Failure Cards**: Generates precise, low-clutter failure summaries when tasks fail, stop, or hit step caps, outlining the task ID, stop reason, last action, last screenshot ID/hash, steps completed, and log suggestion.
5. **Telegram Stop Fast Path**: Clean stop keywords (`停下`, `别动`, `停止操作`, `stop computer`, `cancel computer task`) are routed directly to `computer.stop` at dispatch time, bypassing normal Codex routing to maximize speed.
