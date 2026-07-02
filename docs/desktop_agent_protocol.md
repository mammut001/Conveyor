# Desktop Agent Protocol — Phase 0 Stub

> **Status**: Design stub. No implementation in this task.
> **Phase 0 scope**: model, registry, intent detection, stub tool, card UX. **No** network listener, **no** agent binary, **no** screenshot capture, **no** mouse / keyboard / browser control, **no** Gemini Computer Use call.

This document describes the **shape** of the future local desktop
agent that the Conveyor control plane will speak to. The list
below is the protocol we plan to implement; it is documented here
so future work has a single source of truth and so the phase-0
registry's `capabilities` list can be checked against it without
discovering the design later.

P5.1 wired register + heartbeat. **P5.2** adds local read-only
screenshot observe via `capture-screen-helper` and
`desktop_agent.py --observe-once`. **P5.3** adds remote observe
requests: chat creates pending requests, Mac agent polls
`/desktop/observe/pending`, captures locally, and returns metadata
only. Computer Use control remains future work. Deterministic tools:
`nodes.status`, `computer.status`, `desktop.screenshot.status`,
`desktop.observe.request`, `desktop.observe.status`,
`desktop.observe.cancel`.

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

---

## 6. P5.1 Desktop agent heartbeat

In P5.1, we implemented the heartbeat mechanism between the VPS control plane and the local MacBook agent:

* **VPS (Control Plane)**: Binds to `127.0.0.1:8766` by default. It exposes endpoints:
  - `POST /desktop/register` to register the agent node ID, display name, agent version, and host info.
  - `POST /desktop/heartbeat` to receive periodic heartbeats (default every 30s) and record the last seen time + agent state.
  - `GET /desktop/status` to view runtime status details of registered nodes.
  - Authentication requires header: `Authorization: Bearer <CONVEYOR_DESKTOP_AGENT_TOKEN>`.
* **MacBook (Local Agent)**: Actively connects to the VPS. Periodically posts registration and heartbeats.
* **Online/Offline state**: If no heartbeat is received within `CONVEYOR_DESKTOP_HEARTBEAT_TTL_SECONDS` (default: 90s), `/nodes` or `/node_status` shows the desktop node as `offline`.
* **Cross-Process Status Sharing**: Because the desktop agent server (`desktop_agent_server.py`) and the bot listeners (e.g. `conveyor-feishu-bot`, `conveyor-telegram-bot`) run as separate processes, the runtime status is persisted to a shared JSON file at `settings.codex_memory_root / "state" / "desktop_nodes.json"`.
  - The file contains *only* basic metadata like `node_id`, `display_name`, `agent_version`, `host`, `last_seen_at`, `agent_state`, and `last_action`.
  - **Security Guarantee**: It does *not* contain the token, secrets, screenshots, or any control action data.
  - State updates are written atomically (via a temporary file replacement), and corrupt JSON files are handled gracefully by treating them as empty.

### Deployment & Security


Exposing the HTTP server publicly is **strongly discouraged** without proper authentication, HTTPS reverse proxy, Tailscale, or VPN tunnels. Always use a strong, unique `CONVEYOR_DESKTOP_AGENT_TOKEN` token.

Example startup command on VPS:
```bash
export CONVEYOR_DESKTOP_NODE_ENABLED=true
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
.venv/bin/python desktop_agent_server.py
```

Example startup command on MacBook:
```bash
export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
export CONVEYOR_DESKTOP_NODE_ID=macbook-payton
export CONVEYOR_DESKTOP_NODE_NAME="Payton MacBook"
.venv/bin/python desktop_agent.py
```

> [!IMPORTANT]
> The local agent `CONVEYOR_DESKTOP_NODE_ID` must match the VPS `CONVEYOR_DESKTOP_NODE_ID` (default is `macbook-payton`). Mismatched node IDs will be rejected by the server with HTTP 400. This mismatch check prevents situations where the agent is running but the `/nodes` panel shows the expected MacBook as offline.
>
> P5.2 adds local read-only screenshot observe via `desktop_agent.py --observe-once` when `capture-screen-helper` is configured. Cursor/keyboard controls and Gemini Computer Use are not implemented.

Chat query routing:
* `/nodes` or `MacBook 在线吗` will report the online/offline status, last seen time, and agent state.
* `computer use status` or `/computer_status` will report connection details.
* `/desktop_screenshot_status` and `/screenshot_status` show helper/agent status and latest local metadata only (they do **not** capture a screenshot).
* `/deploy_verify` reports P5.2 deployment readiness (git SHA, helper config, latest metadata; no capture).
* Screenshot observe phrases route to `desktop.screenshot.status` (status only in chat; no remote capture).
* Local capture on Mac: `python desktop_agent.py --observe-once`
* `CONVEYOR_DESKTOP_SCREENSHOT_HELPER` must be an absolute path.
* Remote `POST /desktop/observe/request` returns not implemented in P5.2.
* P5.2.1 supports local observe + metadata status; it does **not** support remote trigger, upload, preview, visual analysis, or Computer Use control.


