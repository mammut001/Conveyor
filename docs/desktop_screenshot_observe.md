# Desktop Screenshot Observe — P5.2 / P5.2.1 / P5.2.2 / P5.3

> **Status**: Implemented (local-first, read-only; P5.3 adds remote observe requests with metadata-only results).
> **Not implemented here**: upload, thumbnail preview, OCR, LLM visual analysis, mouse/keyboard/browser control.
> **Computer Use control** is implemented separately in **P5.6** behind a gated direct mode (cua backend, Mac-local only, OFF by default) — see `docs/desktop_security.md §7`.

P5.2 adds **read-only screenshot observe** on the operator's MacBook. Screenshots stay on disk by default. The VPS control plane receives metadata only when future remote wiring lands; in P5.2 it does not receive image bytes.

## P5.2.1 supports

- Local one-shot screenshot capture on Mac via `python desktop_agent.py --observe-once`
- Metadata listing through `/desktop_screenshot_status` and `/screenshot_status`
- Feishu read-only status card (metadata only; no capture/upload/preview buttons)
- Absolute-path validation for `CONVEYOR_DESKTOP_SCREENSHOT_HELPER`
- Atomic metadata writes and latest-metadata status output

## Execution desktop (current session)

With agents enabled, a generic `截图` / `截屏` or `/observe_request` captures the desktop of the conversation that is executing now. The target is stored on the computer task when the request starts.

- The default agent (display unset), including the canonical main session, uses the configured host computer service (`HttpComputerBackend`). On the VPS that service is `scripts/run-vps-computer.sh`.
- Another agent's own display uses the in-process X11 backend (`:101` and up). A secondary session of that same agent shares that display.
- An archived or missing agent desktop fails closed. It is not replaced with the host desktop.
- Replies name the worker and session, then the real target, for example `Conveyor › 主会话 / VPS共享桌面` plus the configured execution node, or `Alpha › … / Agent独立桌面(:101)`. A node id is not described as a Mac just because it used to say `macbook-payton`.
- A request that names a Mac, MacBook, or a node id other than the configured host is rejected. It does not capture the VPS instead. Naming the configured host, including `vps-desktop`, captures that host only when the conversation is already pinned to it. It does not move an Agent desktop. English text such as "remote desktop" is not treated as a Mac request.
- The computer task is checked against the target resolved before it was created. If `takeover_scope` / `agent_id` or the backend display no longer matches, the task is marked error and nothing is captured.
- The default worker's secondary session (`agent-default-s-…`) uses the shared host desktop when that worker has no display. Another worker's secondary session uses that worker's X11 display. A worker with no display still fails closed. An unbound group reply names the host desktop and does not invent a main-session context.
- Thumbnail delivery uses the physical chat port and the screenshot id from this task. The thumbnail is a new image that fits the configured width, height, and byte limits (ImageMagick `magick`/`convert` when present, otherwise Pillow). The original screenshot is never sent in its place. `CONVEYOR_DESKTOP_UPLOAD_ENABLED=false`, `--metadata-only`, a missing port, or a thumbnail error returns metadata only.
- The Web Console host screenshot stays on the explicit host observe request. It is not retargeted onto an agent desktop.

The legacy observe queue remains for agents-disabled chats and for an explicit Mac whose registration platform is macOS and whose heartbeat sets `poll_observe`. A heartbeat without that flag is not an observe client (`node_does_not_poll_observe`), even if the node is online. `desktop_agent.py` reports the flag from `--poll-observe`.

### VPS node id

`scripts/run-vps-computer.sh` uses `CONVEYOR_DESKTOP_NODE_ID=vps-desktop` and `CONVEYOR_DESKTOP_NODE_NAME=VPS desktop` only when those variables are absent from both the process environment and `/opt/conveyor/.env`. It does not override an id the control plane already configured. `load_dotenv()` does not override variables that are already set, so exporting the client id alone would disagree with the server. After review, run `scripts/migrate-desktop-node-id.sh /opt/conveyor/.env` to replace an exact `macbook-payton` id and an exact `Payton MacBook` name with `vps-desktop` / `VPS desktop` in one transaction. Any other id is left untouched. The backup is mode 0600, created exclusively, and the script prints only the backup path and whether the file changed. Restart the control plane afterwards. The old node record is not deleted; it goes offline because the VPS process no longer updates it. A real Mac observer keeps its own id and must match the control plane if it is the configured desktop node.

## P5.3 Remote Observe Request

**Supported:**

- Chat creates a pending observe request (`/observe_request`, NL phrases like `截图看看我电脑现在是什么`)
- Mac desktop agent polls the VPS (`python desktop_agent.py --poll-observe --poll-computer`)
- Mac captures one local screenshot via `capture-screen-helper`
- Mac returns **metadata only** to the VPS (no image bytes, no base64)
- Chat shows metadata/status (`/observe_status`, `/screenshot_status`)

**Not supported:**

- Image upload, thumbnail preview, visual analysis, OCR
- Mouse, keyboard, browser, or app automation
- Computer Use action execution
- Continuous screen streaming

**Setup:**

On VPS:

```bash
export CONVEYOR_DESKTOP_NODE_ENABLED=true
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
python desktop_agent_server.py
```

On Mac:

```bash
export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
python desktop_agent.py --poll-observe --poll-computer
```

In Feishu:

```text
截图看看我电脑现在是什么
/observe_status
/screenshot_status
```

Request store: `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json`

P5.3.1 hardens the observe request store with a cross-process file lock.
This prevents lost updates when Telegram, Feishu, and desktop_agent_server.py
read/write CODEX_MEMORY_ROOT/state/desktop_observe_requests.json concurrently.


Config:

```env
CONVEYOR_DESKTOP_OBSERVE_REQUEST_TTL_SECONDS=300
CONVEYOR_DESKTOP_OBSERVE_POLL_INTERVAL_SECONDS=5
CONVEYOR_DESKTOP_OBSERVE_MAX_PENDING=3
```

## P5.2.1 does not support (still true for control features)

- Remote desktop control from chat
- Screenshot or thumbnail upload
- Image preview in Feishu cards
- Gemini / GPT visual analysis
- Mouse, keyboard, browser, or app automation
- Computer Use control

## Components

| Piece | Repo | Role |
|---|---|---|
| `capture-screen-helper` | `capture-your-screen` | macOS ScreenCaptureKit CLI; writes PNG + JSON metadata |
| `desktop_screenshot.py` | `Conveyor` | Validates helper output, stores metadata JSON |
| `desktop_agent.py --observe-once` | `Conveyor` | Local one-shot capture entry point |
| `desktop.screenshot.status` | `Conveyor` | Deterministic tool / NL route for observe status |
| `scripts/deploy_verify_p5_2.py` | `Conveyor` | Deployment-readiness checks (no capture) |

## Configuration

`CONVEYOR_DESKTOP_SCREENSHOT_HELPER` must be an **absolute path**. Relative helper paths are refused.

```env
CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
CONVEYOR_DESKTOP_SCREENSHOT_DIR=
CONVEYOR_DESKTOP_SCREENSHOT_MAX_BYTES=5000000
CONVEYOR_DESKTOP_SCREENSHOT_ALLOW_UPLOAD=false
```

- Helper path empty → observe disabled with a clear status message.
- Default screenshot dir: `CODEX_MEMORY_ROOT/desktop/screenshots`.
- Upload remains `false` in P5.2 even if set `true`.

## Local capture

```bash
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
.venv/bin/python desktop_agent.py --observe-once
```

On success the agent prints safe JSON and writes:

- `.../desktop/screenshots/<screenshot_id>.png`
- `.../desktop/screenshots/<screenshot_id>.json`

Metadata fields: `screenshot_id`, `path`, `sha256`, `width`, `height`, `display_id`, `created_at`, `node_id`, `helper_version`, `bytes`.

## Status commands (metadata only)

These commands show helper/desktop-agent status and the latest local metadata. They do **not** capture a screenshot:

- `/desktop_screenshot_status`
- `/screenshot_status`
- `/deploy_verify` — deployment-readiness summary (also does not capture)
- Natural language: `截图状态`, `最近的截图`, `desktop screenshot status`

Capture phrases such as `截图看看我电脑现在是什么` route to `desktop.observe.request` and create a remote observe request (async — the Mac agent completes via polling).

Status phrases such as `截图状态` route to `desktop.observe.status`.

Local one-shot capture remains:

```bash
python desktop_agent.py --observe-once
```

Remote observe polling:

```bash
python desktop_agent.py --poll-observe --poll-computer
```

No base64, OCR text, window titles, secrets, or prompt content are stored.

## Deployment checklist

### On VPS

```bash
cd /opt/conveyor
git fetch origin
git reset --hard origin/main
git rev-parse HEAD
.venv/bin/python scripts/deploy_verify_p5_2.py
sudo systemctl restart conveyor-telegram-bot conveyor-feishu-bot
```

### On Mac

```bash
cd /path/to/capture-your-screen
bash scripts/build_helper.sh
sudo cp build/Release/capture-screen-helper /usr/local/bin/capture-screen-helper

cd /path/to/Conveyor
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
export CONVEYOR_DESKTOP_SCREENSHOT_DIR="$HOME/.codex/desktop/screenshots"
python desktop_agent.py --observe-once
```

### In Feishu

```text
/screenshot_status
```

Expected:

- Latest local screenshot metadata is shown
- No image is uploaded
- No remote capture is attempted

## Screen Recording permission

macOS requires **Screen Recording** permission for `capture-screen-helper`. Grant it under **System Settings → Privacy & Security → Screen Recording**.

Check without capturing:

```bash
capture-screen-helper --check-permission --json
```

This is a **manual test** — automated smokes do not request Screen Recording permission.

## Natural language routing

Phrases such as `截图看看我电脑现在是什么`, `看一下 MacBook 屏幕`, and `take a screenshot on my desktop` route to `desktop.screenshot.status`. They do **not** start Codex and do **not** claim a screenshot was captured unless one actually was.

## Remote observe HTTP API (agent polling)

| Endpoint | Method | Purpose |
|---|---|---|
| `/desktop/observe/pending?node_id=...` | GET | Agent fetches pending requests |
| `/desktop/observe/claim` | POST | Agent claims a pending request |
| `/desktop/observe/complete` | POST | Agent submits metadata-only result |
| `/desktop/observe/fail` | POST | Agent reports capture failure |
| `/desktop/observe/request` | POST | **Disabled (501)** — chat creates requests internally |

All endpoints require Bearer token authentication.

## Safety

- Read-only screenshot only
- No mouse, keyboard, clipboard, browser, or app automation
- No automatic upload
- No Gemini / LLM visual analysis
- No OCR
- No continuous capture stream
- Manual/local invocation first
- Explicit logs when observe runs (`conveyor.desktop_screenshot`)

**Computer Use control is still not implemented.**
