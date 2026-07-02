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
4. **No screenshot, click, typing, browser control, password
   entry, payment action, file deletion, or form submission
   is implemented in this task.** The protocol design in
   `docs/desktop_agent_protocol.md` lists the future surface;
   none of it is in code.
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
9. **Computer Use is never automatic.** A natural-language
   "open Xcode on my Mac" message must never execute a step
   without the operator pressing a confirm button (or sending
   the confirmation text). Codex must not "decide" to call
   the agent.

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
