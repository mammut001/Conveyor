# Desktop Agent Protocol — Phase 0 Stub

> **Status**: Design stub. No implementation in this task.
> **Phase 0 scope**: model, registry, intent detection, stub tool, card UX. **No** network listener, **no** agent binary, **no** screenshot capture, **no** mouse / keyboard / browser control, **no** Gemini Computer Use call.

This document describes the **shape** of the future local desktop
agent that the Conveyor control plane will speak to. The list
below is the protocol we plan to implement; it is documented here
so future work has a single source of truth and so the phase-0
registry's `capabilities` list can be checked against it without
discovering the design later.

Nothing in this file is wired up in code yet. The only thing that
exists today is the deterministic stub tool `computer.status` that
returns a "not implemented" message and the `nodes.status` tool
that lists the registered nodes.

---

## 1. Architectural separation

```
Conveyor control plane (VPS)                Local desktop (operator's MacBook)
   |                                                |
   |  Telegram / Feishu message                    |
   |  -> handlers/intent.py detects                |
   |     "computer use" / desktop intent           |
   |  -> routes to nodes.status / computer.status   |
   |     (read-only stubs in phase 0)               |
   |                                                |
   |  -- future, post phase 0 --                   |
   |  WebSocket / HTTPS to local agent             |
   |  <---------------------------------------------->  desktop agent
   |                                                |  (Python or Swift,
   |                                                |   runs as user, not root)
```

The agent runs **on the operator's machine**, not on the VPS. The
VPS never makes outbound network calls to the operator's laptop
in phase 0; the future direction is an inbound connection from
the laptop to the control plane (VPS as the listener) so the
firewall rules stay operator-friendly.

---

## 2. Planned message surface

All messages are JSON. The shape is intentionally tiny in phase 0
so it is easy to implement incrementally. None of these are wired
up yet.

### 2.1 Registration / heartbeat

* `desktop.agent.register` — laptop → VPS, first connection.
  Body: `{node_id, display_name, host, agent_version, auth_token}`.
  VPS replies with `{ok, allowed: bool}` after validating the
  `CONVEYOR_DESKTOP_AGENT_TOKEN` shared secret.

* `desktop.agent.heartbeat` — laptop → VPS, every 30 s. Body:
  `{node_id, ts, last_action, agent_state}`. VPS updates the
  registry entry's `last_seen_at` and `status` accordingly.

### 2.2 Task lifecycle

* `desktop.task.create` — VPS → laptop, request a new task.
  Body: `{task_id, intent, payload, require_step_confirm}`. Laptop
  replies with `{ok, task_id}` or `{error, reason}`.

* `desktop.task.status` — VPS → laptop, poll for current state.
  Body: `{task_id}`. Laptop replies with
  `{state, last_step, queued_steps, screenshot_ref?}`.

* `desktop.task.stop` — VPS → laptop, kill the current task.
  Body: `{task_id, reason}`. Laptop replies with `{ok, cleanup}`.

### 2.3 Screenshot / observe

* `desktop.screenshot.latest` — VPS → laptop, request a fresh
  frame. Body: `{region?, max_bytes?}`.
  Laptop replies with `{ok, png_bytes, hash, width, height,
  captured_at}` or `{error, reason}`.

* `desktop.observe` — VPS → laptop, ask the agent to summarise
  what is on screen. Body: `{focus?, max_words?}`.
  Laptop replies with `{ok, text, regions: [...]}` — the regions
  list is the agent's structured guess at UI elements so the
  control plane can reason about them.

### 2.4 Step proposal / confirmation (Computer Use loop)

* `desktop.step.propose` — VPS → laptop, push a proposed step
  (e.g. `click(120, 340)` or `type("hello")`). Body:
  `{task_id, step_kind, args, screenshot_hash_before, model_intent}`.
  Laptop replies with `{ok, preview_screenshot_hash, accepted}`. The
  VPS does not let Codex auto-execute a step; the operator must
  confirm.

* `desktop.step.confirm` — VPS → laptop, the operator approved
  the step. Body: `{task_id, step_id, approved: bool, note?}`.
  Laptop replies with `{ok, executed, screenshot_hash_after, error?}`.

### 2.5 Stop / safety

* `desktop.task.stop` — see above.
* A heartbeat that goes silent for `> 2 × heartbeat_interval` is
  treated as offline and any in-flight task is **paused** (not
  cancelled) so a flaky network does not destroy partial work.

---

## 3. Trust + auth

The shared secret `CONVEYOR_DESKTOP_AGENT_TOKEN` (a long random
string in `.env`) is the only auth the local agent presents. The
VPS will:

1. validate the token on every register / heartbeat,
2. require the same allowlist gate (`is_allowed`) on every
   step-confirm callback, and
3. bind each task to the (operator_id, chat_id, channel) tuple
   that started it.

The agent is always treated as **untrusted for state-changing
actions** — every step requires operator confirmation through
the existing `handlers.tools.confirm` token system, not just
the chat binding. See `docs/desktop_security.md` for the full
contract.

---

## 4. What is explicitly out of scope for phase 0

These exist in the protocol design but are **not** in code yet:

- No WebSocket / HTTP server on the VPS.
- No agent binary (Swift / Python / Go / Tauri — to be decided).
- No real screenshot capture (`screencapture`, `pyautogui`,
  `mss`, etc.).
- No mouse / keyboard synthesis.
- No browser automation (Selenium, Playwright, Chrome DevTools
  Protocol, etc.).
- No Gemini Computer Use call.
- No clipboard access.
- No file-system write from the agent.
- No camera / microphone access.
- No payment / banking flows.

If a future task wires any of these, the registry must flip
`is_stub_environment()` to `False` and the new capabilities
(`browser.control`, `mouse.click`, `keyboard.type`,
`computer_use.step`) must replace the stub surface before any
operator sees a working button.

---

## 5. Open questions (for future tasks)

- The user is the operator **and** the desktop owner. Do we still
  need a second-factor confirm for first-time registrations?
- Should screenshot bytes ever leave the laptop, or should the
  agent pre-process into text + region coordinates and only send
  those? (Privacy argument says: never send raw screenshots.)
- Can a future operator run multiple desktops (e.g. work + home)?
  The registry already supports `nodes.status` listing them; the
  routing layer would need a "target node" hint in the natural
  language intent.
