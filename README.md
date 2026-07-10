# Conveyor

**Run Codex from Telegram or Feishu on your own VPS.**

Conveyor turns a private Telegram or Feishu chat into a remote control panel for
[Codex CLI](https://github.com/openai/codex) running on your own server — a
self-hosted, single-operator **AI coding assistant** you drive from your phone.
Send a message from your phone, let Codex inspect your repo, fix code, check
logs, create reminders, search your personal tools, and report back — no SaaS
dashboard, no shared workspace, no multi-user complexity. It's a remote coding
agent for people who already trust Codex and just want a phone-to-agent
interface to their own private developer tools.

> **Single trusted operator only.** Conveyor is built for one human, one VPS,
> one whitelisted chat per channel. It is *not* a multi-tenant bot and never
> will be. If you need a public agent, look elsewhere.

[中文文档 / Chinese](README.zh.md) · [Architecture](docs/architecture.en.md) · [License](LICENSE)

> [!IMPORTANT]
> **v0.1.1 Security Hardening Release**: Conveyor v0.1.1 is a security hardening release that addresses recent security audit recommendations. Operators should run `make smoke` after updating. Because systemd unit files now have narrowed `ReadWritePaths` configuration, you **must reinstall and reload** systemd units upon upgrade. See the deployment section for exact commands.

---

## Why this exists

You are away from your laptop. Your VPS or dev box has the repo, the Codex CLI,
the running services, the failing test, and the missing environment variable
you need to fix.

Conveyor is the bridge:

- **You** stay on Telegram or Feishu, on your phone, in plain language.
- **Codex** runs on your VPS, in a detached git worktree, with the full workspace.
- **Conveyor** carries the message there, carries the answer back, and gives
  you `/diff`, `/apply`, and `/cancel` so you stay in control of what lands
  in the main repo.

No public agent. No shared cloud workspace. No "invite your team" workflow.
Just your chat, your server, and your coding agent.

---

## A demo flow (illustrative)

You:

> `/run fix the failing test in tests/test_parser.py and show me the diff`

Conveyor:

> ⏳ Codex job started on VPS … worktree `day-2026-07-01`

A minute later, Codex replies with a short summary. Then you:

> `/status`  → current job + worktree path
>
> `/diff`    → `git status` + truncated diff from the worktree
>
> `/apply`   → merge the worktree back to main (only when main is clean)
>
> `/cancel`  → kill the running Codex process if you change your mind

For lightweight ops, plain text works too:

> `看看磁盘`  →  disk usage on `/`, `/srv`, `/opt`
>
> `为什么服务器这么慢`  →  collect load/ps/disk facts, then ask Codex
>
> `提醒我 10 分钟后看 build`  →  scheduler reminder, delivered to your chat

This is an **illustrative** flow. Exact wording is whatever your Codex instance
returns; the protocol around it — acknowledgement, status, diff, apply,
cancel — is what Conveyor guarantees.

---

## What Conveyor can do

A single surface, the same on Telegram and Feishu, all backed by an auditable
agent tool layer (`Agent tool layer` — see `docs/architecture.en.md`).

### Remote Codex control

- Plain text or `/run <prompt>` / `/fix <prompt>` — kick off a Codex job in a
  detached worktree (sandbox: `danger-full-access` by design; see Safety).
- `/status` · `/last` · `/jobs [n]` — current / most recent jobs.
- `/diff` — `git status` + truncated diff from the latest worktree.
- `/apply` — merge the worktree back to main (only when main is clean).
- `/discard` · `/cancel` — drop the worktree or kill the running process.
- `/queue` · `/queue_cancel` · `/queue_clear` · `/queue_pause` · `/queue_resume`
  — persistent single-concurrency FIFO job queue for Codex using SQLite (survives bot restarts and VPS reboots; previously running jobs become interrupted on startup, queued jobs auto-resume, and pause state persists).

### Personal memory

- `/memo <text>` or `记 <text>` — write to today's `MEMORY.md` (no Codex,
  no worktree).
- `/memory [date] [category]` · `/journal [n]` — read MEMORY and archived
  journals.

### Personal tools on the VPS

- Notes: `/note <text>` · `/notes [query]`
- Reminders: `/remind <text + time>` · `/reminders`
- Scheduler health: `/scheduler_status` · `/scheduler_probe` · `/scheduler_probe_live`
- Daily Briefing: `/brief_today` · `/brief_tomorrow` · `/brief_settings` ·
  `/brief_enable [HH:MM]` · `/brief_disable` · `/brief_probe`

> **Feishu users:** Codex job starts, finishes, failures, `/diff`
> previews, dangerous-action confirmations, and execution-node
> status render as interactive message cards with tap-to-act buttons
> (Status / Diff / Apply / Discard / Cancel / Confirm / Refresh).
> The buttons are convenience wrappers around the same commands —
> see [Feishu setup](#feishu-setup).

### Diagnostics & ops

- `/health [full] [json] [nosecurity]` · `/doctor` · `/diag [since]`
- `/audit [stale-min]` · `/security [since]` · `/ratelimit [n]` ·
  `/audit_tools [n]`
- `/metrics [n]` · `/log [sel]` · `/meta [sel]`
- `/tools` · `/diagnose [server|bot|logs|quick]` · `/restart telegram|feishu|maintain`
- `/maintain [keep]` · `/clean [keep]` · `/smoke` · `/editcheck`
- VPS fast path: `/load` · `/vps` · `/htop` · `/ps` · `/disk` · `/logs` ·
  `/service_status` · `/git_status`

### Optional integrations (only as configured)

- **Gmail** via IMAP / SMTP App Password —
  `/gmail_status` · `/gmail_recent` · `/gmail_search` · `/gmail_read` ·
  `/email_send`
- **Google Calendar & Contacts** via OAuth —
  `/auth_google` · `/google_status` · `/calendar_today|week|search|freebusy|create` ·
  `/contacts_search`
- **GitHub** issues / PRs / CI (read-first) —
  `/github_status` · `/github_issues` · `/github_prs` · `/github_ci` ·
  `/github_create_issue` · `/github_comment`
- **Local file search & knowledge base** —
  `/files_roots` · `/files_search` · `/files_read` ·
  `/kb_index` · `/kb_status` · `/kb_search` · `/project_docs`
- **Web fetch / search / research** —
  `/web_fetch` · `/web_text` · `/web_search` · `/research` · `/project_research`
- **Project profiles** & planning —
  `/projects` · `/project_add` · `/project_use` · `/project_status` ·
  `/project_health` · `/project_roadmap` · `/project_next` ·
  `/project_release_checklist` · `/plan_today` · `/plan_dev` · `/inbox_triage`
- **Natural-language routing** — most of the above also works as plain
  language: `看看负载`, `为什么服务器慢`, `搜一下 GitHub issue`, etc. Try
  `/nl_help` for the full list.

### Execution nodes (VPS + desktop stub)

Conveyor is becoming a private control plane for your VPS and,
eventually, your local desktop. The control plane always runs on
the VPS. The desktop node is a **stub** in this phase: it shows
up in `/nodes` only when you opt in, and it is always
  `offline` until heartbeats arrive. **P5.2** adds read-only local
  screenshot observe via `capture-screen-helper`; mouse, keyboard,
  browser control, and Computer Use are **not** implemented by
  default — **P5.6** adds a gated *direct computer-use mode* (cua
  backend, Mac-local only) that is **OFF by default**.

- `/nodes` · `/node_status` — list known execution nodes, their
  capabilities, and dynamic online/offline status.
- `/computer_status` — show Computer Use status (enabled flag, direct-mode
  source, Cua driver probe, active task).
- Natural language: `我的节点`, `机器状态`, `主机状态`,
  `MacBook 在线吗`, `desktop node`, `nodes status`,
  `computer use status`. Desktop-target phrases like
  `帮我在 Mac 上打开 Xcode`, `操作电脑…`, `帮我点…`, `打开 Chrome`,
  `在电脑上…` route to `computer.task` (the direct Cua loop) **only
  when direct mode is armed**; otherwise they return the stub reply.
  Screenshot observe phrases like `take a screenshot on my desktop`
  route to `desktop.observe.request` (P5.3 remote observe — metadata
  only). Status phrases like `截图状态` route to `desktop.observe.status`.

### P5.6 Direct Computer Use Mode (cua backend)

A hands-free direct computer-use mode: `Telegram/Feishu NL → Codex →
Conveyor computer-use tools → Mac desktop_agent → local cua-driver →
real desktop actions`. The backend is `trycua/cua`, run **only on the
Mac desktop agent** — the VPS never speaks the Cua protocol.

Install Cua on the Mac agent side only. The official driver exposes
`cua-driver mcp` as an MCP stdio server, but Conveyor's local wrapper
executes the same binary through `cua-driver call <tool> <json>` so no
Cua server is exposed on the network. On macOS, grant the driver before
expecting real observe/click/type to work:

```bash
cua-driver permissions grant
cua-driver permissions status --json
```

`/computer_status` reports the driver path/version and this metadata-only
permission status. If permissions are not granted, Cua may start but
screen capture or input actions will fail.

After granting permissions, run the read-only local verifier on the Mac:

```bash
python3 scripts/cua_driver_real_smoke.py --cmd "cua-driver mcp"
```

**All flags default to `false`/disabled.** The mode is opt-in:

- **Arm (TTL)**: `/computer_arm [minutes]` enables direct mode for a
  limited time. After expiry, tasks are blocked.
- **Direct gate**: `CONVEYOR_COMPUTER_DIRECT_ENABLED=true` is required
  for `/computer_arm`, `/computer_task`, `/computer_action`, and for
  `is_direct_mode_active`. `USE_ENABLED` alone only unlocks status /
  observe readiness.
- **Always-direct**: `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true` bypasses
  arming only when both `CONVEYOR_COMPUTER_USE_ENABLED=true` and
  `CONVEYOR_COMPUTER_DIRECT_ENABLED=true`.
- **Kill switch**: `/computer_stop` cancels the active task
  immediately.

**Commands**

- `/computer_status` — enabled flag, direct-mode source, Cua probe,
  active task.
- `/computer_arm [minutes]` — arm direct mode for a TTL (e.g.
  `/computer_arm 30`).
- `/computer_task <goal>` — run the Codex→Cua loop hands-free (e.g.
  `/computer_task 打开 Chrome 并访问 conveyor.dev`). Fails fast if
  direct mode is not active.
- `/computer_stop` — cancel the active task immediately.
- `/computer_log [task_id]` — show the redacted trajectory of a task.
- `/computer_screenshot` — capture one desktop observation
  (metadata/screenshot id) in direct mode.
- `/computer_observe` — trigger one desktop observation.
- `/computer_action <json>` — execute a single allow-listed action,
  e.g. `{"action":"click","x":100,"y":100}`. The Cua backend also
  accepts optional `pid`/`window_id`/`element_index`/`element_token`/
  `delivery_mode` for more reliable app-local clicks when the planner
  has window state.

**Key env flags** (all default safe)

| Env | Default | Meaning |
|---|---|---|
| `CONVEYOR_COMPUTER_USE_ENABLED` | `false` | Master enable (whole feature). |
| `CONVEYOR_COMPUTER_DIRECT_ENABLED` | `false` | Enable direct (hands-free) mode. |
| `CONVEYOR_COMPUTER_ALWAYS_DIRECT` | `false` | Bypass arming (TTL) when true. |
| `CONVEYOR_COMPUTER_MAX_STEPS` | `20` | Max steps per task. |
| `CONVEYOR_COMPUTER_MAX_SECONDS` | `600` | Max wall-clock seconds per task. |
| `CONVEYOR_CUA_DRIVER_CMD` | `cua-driver mcp` | Mac-local cua driver command. Conveyor uses the first token as the local `cua-driver` binary for `call`/status operations. |
| `CONVEYOR_COMPUTER_ALLOWED_ACTIONS` | `observe,click,type,hotkey,scroll,wait` | Action allow-list. |
| `CONVEYOR_COMPUTER_BLOCKED_KEYWORDS` | `password,passcode,bank,payment,crypto,keychain,system settings,delete account` | Stop-on-match guard. |
| `CONVEYOR_COMPUTER_BACKEND` | `http` | `http` (real Mac agent) or `fake` (in-process, for tests). |

**Safety envelope (enforced even in direct mode)**: action allow-list,
blocked-keyword guard, no secret injection, typed-text/hotkey
redaction in all stored logs, result allow-list from the driver,
`MAX_STEPS`/`MAX_SECONDS` caps, and the `/computer_stop` kill switch.
Cua never crosses the network. See `docs/desktop_security.md §7`.


### P5.1 Desktop Agent Heartbeat

In P5.1, the desktop agent registration and heartbeat protocol is active:
* **VPS**: Binds to `127.0.0.1:8766` by default. Start server with:
  ```bash
  export CONVEYOR_DESKTOP_NODE_ENABLED=true
  export CONVEYOR_DESKTOP_AGENT_TOKEN=...
  .venv/bin/python desktop_agent_server.py
  ```
* **MacBook Node**: Actively registers and heartbeats to VPS. Start agent with:
  ```bash
  export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
  export CONVEYOR_DESKTOP_AGENT_TOKEN=...
  export CONVEYOR_DESKTOP_NODE_ID=macbook-payton
  export CONVEYOR_DESKTOP_NODE_NAME="Payton MacBook"
  .venv/bin/python desktop_agent.py
  ```

* **Cross-Process Status Sharing**: The agent server and the bot listeners share the heartbeat state through the JSON file at `CODEX_MEMORY_ROOT/state/desktop_nodes.json`. This stores *only* connection metadata; no tokens, secrets, or screenshots are written.
* **Node ID Validation**: The MacBook agent's `CONVEYOR_DESKTOP_NODE_ID` must match the VPS `CONVEYOR_DESKTOP_NODE_ID` (default is `macbook-payton`). Mismatching requests will be rejected with HTTP 400.


### P5.2 Desktop Screenshot Observe (read-only)

* **Helper**: Build `capture-screen-helper` from the `capture-your-screen` repo and set `CONVEYOR_DESKTOP_SCREENSHOT_HELPER` to an absolute path.
* **Local capture (Mac)**: `python desktop_agent.py --observe-once`
* **Status/metadata only**: `/desktop_screenshot_status`, `/screenshot_status`, or phrases like `截图状态` / `最近的截图`. These do **not** capture a screenshot.
* **Deploy verify**: `/deploy_verify` or `scripts/deploy_verify_p5_2.py` — readiness checks without capture.
* **Feishu**: read-only status card (Refresh / Nodes only; no capture, upload, or preview).
* Screenshots stay under `CODEX_MEMORY_ROOT/desktop/screenshots/` by default. Upload is disabled in P5.2.

**P5.2.1 supports:** local one-shot capture on Mac, metadata status commands, Feishu status card.

**P5.2.1 does not support:** upload, thumbnail preview, visual analysis, mouse/keyboard/browser control, Computer Use.

### P5.3 Remote Observe Request (metadata only)

* **Chat request**: `/observe_request`, `/screenshot_request`, or NL like `截图看看我电脑现在是什么`
* **Mac polling**: `python desktop_agent.py --poll-observe --poll-computer` (register + heartbeat + observe / Computer Use poll loop)
* **Status**: `/observe_status`, `/screenshot_status`, or NL like `截图状态`
* **Cancel**: `/observe_cancel <request_id>` for pending/claimed requests
* VPS stores pending requests at `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json`
* P5.3.1 hardens the observe request store with a cross-process file lock. This prevents lost updates when Telegram, Feishu, and `desktop_agent_server.py` read/write `CODEX_MEMORY_ROOT/state/desktop_observe_requests.json` concurrently.
* Mac captures locally; only metadata crosses to VPS — **no image upload**


**P5.3 does not support:** image upload, thumbnail preview, visual analysis, OCR, mouse/keyboard/browser control, Computer Use.

**Deployment (VPS):**

```bash
export CONVEYOR_DESKTOP_NODE_ENABLED=true
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
python desktop_agent_server.py
```

**Deployment (Mac polling):**

```bash
export CONVEYOR_CONTROL_PLANE_URL=https://your-control-plane.example.com
export CONVEYOR_DESKTOP_AGENT_TOKEN=...
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
python desktop_agent.py --poll-observe --poll-computer
```

**Deployment (VPS bot update):**

```bash
cd /opt/conveyor
git fetch origin && git reset --hard origin/main
git rev-parse HEAD
.venv/bin/python scripts/deploy_verify_p5_2.py
sudo systemctl restart conveyor-telegram-bot conveyor-feishu-bot
```

**Deployment (Mac):** build `capture-screen-helper`, set absolute `CONVEYOR_DESKTOP_SCREENSHOT_HELPER`, run `python desktop_agent.py --observe-once`. Screen Recording permission is a manual macOS step.

> **Computer Use control is gated and OFF by default (P5.6).** The direct mode is opt-in via `/computer_arm` or `CONVEYOR_COMPUTER_ALWAYS_DIRECT=true`, with a hard safety envelope (action allow-list, blocked-keyword guard, redaction, caps, kill switch). Cua runs only on the Mac agent. See `docs/desktop_security.md §7`.


### P5.4 Manual Screenshot Thumbnail Upload

* **Explicit Request Flow**: Operator manually requests preview/thumbnail upload via `/observe_upload <request_id>` or `/screenshot_upload <screenshot_id>`, or using natural language phrases like `发一下刚才的截图` or `把刚才截图发我`.
* **Thumbnail Scaling & Resizing**: The Mac agent generates a downscaled thumbnail from the original screenshot using macOS native `sips` tool (scaled to maximum 1280x800, under 750 KB size limit). The full-resolution screenshot remains strictly local on the MacBook.
* **VPS Outbound Port Delivery**: Once a completed thumbnail is received, the control plane sends the preview image to the original chat via the Feishu/Telegram outbound port `send_image` API.
* **Cleanup Control**: Temporary VPS upload files can be cleaned up using `/upload_cleanup` which deletes files older than `CONVEYOR_DESKTOP_UPLOAD_RETENTION_SECONDS`.
* **State Persistence**: Pending, claimed, and completed requests are tracked in `CODEX_MEMORY_ROOT/state/desktop_upload_requests.json`.

### P5.4.1 Hardening & Validation Constraints

P5.4.1 adds a hardening pass with strict validation checks:
* **File Type Magic Validation**: The control plane checks that files uploaded to `/desktop/upload/complete` have a PNG magic header (`\x89PNG\r\n\x1a\n`) and either `application/octet-stream` or `image/png` content-types before writing.
* **Absolute Temp Directory Enforcement**: If `CONVEYOR_DESKTOP_UPLOAD_TEMP_DIR` is configured, it must be an absolute path. Relative paths are rejected and fallback to default safe absolute directory (`CODEX_MEMORY_ROOT/desktop/uploads`).
* **Symlink and Path Traversal Protections**: 
  - **VPS Upload Directory**: Temp files are written atomically (`.tmp` replacement) to a filename derived strictly from the server-side `upload_id` and verified to reside inside the upload directory.
  - **VPS Cleanup**: The cleanup loop rejects relative or invalid directories, refuses to follow symlinks, skips directories, and verifies that the resolved paths of files to delete are strictly within the resolved temporary upload directory.
  - **Mac Agent Local Screenshot Source**: Resolving the source screenshot path (`resolve_local_screenshot_source`) checks that `screenshot_id` is safe (alphanumeric/hyphen/dots, length <= 128, no `..` or `/`). It requires the resolved path to be absolute, reside strictly within `conveyor_desktop_screenshot_dir`, be a regular file, not be a symlink, and match the metadata SHA-256 (if metadata exists).
* **Correct `source_screenshot_id` mapping**: The VPS looks up the screenshot ID from the upload request database using the `upload_id` parameter to record it as `source_screenshot_id`, preventing the upload ID from being used incorrectly as the screenshot ID.
* **Atomic Delivery Marking**: Outbound image delivery is decoupled from long-lived database locks to prevent blocking concurrent network I/O. Upon successful delivery, the status is marked atomically using lock-guarded helper functions.



Every feature above is already shipped in this repo. If a capability is not
listed here, it does not exist yet.

---

## Safety model

Conveyor is a **single-operator private VPS control surface**, not a
multi-tenant SaaS. The model is intentionally simple and honest:

- **Single trusted operator.** One Telegram user id, one Feishu open id, one VPS.
- **Sender allowlist.** Channel access is denied unless the sender id exactly
  matches `TELEGRAM_ALLOWED_USER_ID` or `LARK_ALLOWED_OPEN_ID`. That allowlist
  is the only thing standing between this bot and the public internet.
- **Secrets stay in `.env`.** Tokens, App Passwords, OAuth refresh tokens, and
  API keys are read by the bot, never echoed in chat, logs, audit logs, or
  `repr()`. `chmod 600 .env` after editing.
- **No SaaS dashboard.** No web UI, no multi-user server, no hosted control
  plane. The interface *is* your chat app.
- **Dangerous actions require confirmation.** Service restarts, scheduler live
  probes, email sending, calendar creation, GitHub commenting — all need an
  explicit `确认执行` / `confirm` or an inline button. Casual `好` / `ok` / `是`
  is intentionally **not** accepted.
- **Confirmations are bound to the originating chat and channel.** A pending
  action confirmed from a different chat or channel is rejected.
- **Write and destructive actions are audit-logged.** Every `WRITE_SAFE`,
  `WRITE`, and `DESTRUCTIVE` tool call appends a redacted JSONL entry to
  `audit/tools.log`. Inspect with `/audit_tools [n]`.
- **`/run` and `/fix` may run Codex with powerful workspace access.** This
  is by design — chat-first control of your own box is the whole point — so
  you must not expose the bot to untrusted people.
- **No commit, no push, no merge.** Apply is always an explicit `/apply` after
  you review `/diff`. The bot never touches `main` without you saying so.
- **Safe Apply & Isolation Pass.** Per-job isolated worktrees, strict changed-file allowlist/denylist checks, session prompt injection guards, and Feishu strict mode are enforced. See [Apply Safety Policy](docs/apply_safety.md) for full details.
- **No Computer Use control.** P5.2 adds read-only local screenshot observe only. Mouse/keyboard/browser automation and Gemini Computer Use calls are intentionally *not* implemented.

This is personal infrastructure, not a public chatbot. Treat it accordingly.

---

## Quick start (10 min)

**Prerequisites:** an Ubuntu VPS with SSH access, [`codex`
CLI](https://github.com/openai/codex) installed, and a Telegram account.

### 1. Install (from your laptop)

```bash
git clone https://github.com/mammut001/conveyor.git && cd conveyor
sudo bash scripts/install.sh
```

The installer will:

1. Install system dependencies.
2. Sync code to `/opt/conveyor`.
3. Create a Python `.venv`.
4. Prompt you to fill in `.env`.
5. Install and start the systemd services.

### 2. Configure `.env`

```bash
sudo nano /opt/conveyor/.env
```

Minimum required:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:from_botfather
TELEGRAM_ALLOWED_USER_ID=your_user_id
CODEX_WORKSPACE_ROOT=/path/to/your/repo
```

`chmod 600 .env` after editing. The systemd unit reads it via
`EnvironmentFile=` and never echoes values.

### 3. Restart and test

```bash
sudo systemctl restart conveyor-telegram-bot
sudo systemctl status conveyor-telegram-bot
```

Open Telegram, send `/start` to your bot, then try `/run hello`. Done.

### 4. Update later

```bash
cd conveyor && git pull
sudo bash scripts/install.sh --update
```

### 5. Optional: add Feishu

See the [Feishu setup](#feishu-setup) section below.

---

## Feishu setup

Same `conveyor` install as above; just configure Feishu as a second channel.

### Get credentials

In the [飞书开放平台](https://open.feishu.cn/app) developer console, create
an app, then:

- **凭证与基础信息** → copy `App ID` and `App Secret`

These go into `LARK_APP_ID` and `LARK_APP_SECRET` in `.env`.

### Enable the 机器人 capability

**应用能力 → 机器人 → 启用** — off by default. Without it the
`im.message.receive_v1` event will not appear in the event list.

### Open the scopes you need

**权限管理** — search and add:

| Search keyword | Scope | Why |
|----------------|-------|-----|
| `p2p` | `im:message.p2p_msg:readonly` | Receive DMs |
| `send_as_bot` | `im:message:send_as_bot` | Send messages as the bot |
| `group_at` | `im:message.group_at_msg:readonly` | Group @-mention (optional) |
| `user.id` | `contact:user.id:readonly` | Sender info resolution (recommended) |

### Subscribe to the event

- **事件订阅** → Subscription method = **长连接 / persistent connection**
- The local bot must be **already running** before you can save the subscription
- Add event: `im.message.receive_v1` (**Receive message**), under
  **应用身份** (not 机器人身份 — that's a different long-connection type)

### Configure `.env`

```dotenv
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
# LARK_ALLOWED_OPEN_ID=ou_xxx
```

The Feishu bot runs in **bootstrap mode** until `LARK_ALLOWED_OPEN_ID` is set:
it will accept any sender, reply with the sender's `open_id`, and ask you to
put that into `.env` then restart. One-time handshake, so you don't have to
fish the open_id out of the log.

After you write the open_id:

```bash
sudo systemctl restart conveyor-feishu-bot
sudo journalctl -u conveyor-feishu-bot -f
```

You should see `connected to wss://msg-frontier.feishu.cn/ws/v2...`.

### Publish and install

**版本管理与发布** → create a new version → 审核 (usually instant for
internal apps) → **申请发布** → **安装到企业**. Scope changes do NOT take
effect on the live app until a new version is published.

### Full setup checklist

- [ ] Credentials: `LARK_APP_ID` and `LARK_APP_SECRET` in `.env`
- [ ] **应用能力 → 机器人 → 启用**
- [ ] `im:message.p2p_msg:readonly` enabled
- [ ] `im:message:send_as_bot` enabled
- [ ] (optional) `im:message.group_at_msg:readonly` enabled
- [ ] (optional) `contact:user.id:readonly` enabled
- [ ] Event subscription: method = **长连接**, identity = **应用身份**, event = `im.message.receive_v1`
- [ ] Local bot is running before saving the subscription
- [ ] New version created, published, installed to enterprise
- [ ] VPS: `.env` has `LARK_*` (no secrets in repo)
- [ ] VPS: `pip install -r requirements.txt` ran (includes `lark-oapi>=1.4.0`)
- [ ] VPS: systemd units installed and active
- [ ] `journalctl -u conveyor-feishu-bot -f` shows `wss://msg-frontier.feishu.cn/...` connected
- [ ] DM the bot, get bootstrap reply, paste `LARK_ALLOWED_OPEN_ID` into `.env`, restart

### Optional: card action callback event

Conveyor uses Feishu interactive message cards for high-value moments:
job started / finished / failed, diff previews, and dangerous-action
confirmations. The card buttons are convenience wrappers around the
same slash commands (`/status`, `/diff`, `/apply`, `/discard`,
`/cancel`) and the existing token-based confirmation system.

To enable button clicks, add **one more event** to the subscription
list above:

- **应用身份**, event: `card.action.trigger` (**Card callback interaction**)

Without it, cards still render and the chat is fully usable — only
button clicks won't reach the bot (you'd fall back to typing the
slash command or `确认执行` / `取消` text). With it, the same
allowlist and confirmation binding that gates typed messages also
gates card presses, so an unauthorized sender cannot trigger an
action by tapping a button.

If the developer console complains about an "invalid callback
address" on this event, the Feishu console is asking for an HTTP
webhook URL — that's the legacy channel. Conveyor uses the long
connection, so leave the URL blank and the event still works.

---

## Who is this for?

- **Solo developers** who already live in a chat app and want Codex on tap.
- **People running Codex CLI on a VPS** and tired of SSH-ing back just to run a
  quick edit or check a log.
- **People who want phone-to-agent workflows** — away from the laptop, but
  still able to fix a failing test, restart a service, or triage an inbox.
- **People who want private personal automation** — notes, reminders,
  briefings, daily briefings, and Codex, all behind one allowlisted chat.
- **People who prefer self-hosted tools** — your `.env`, your VPS, your
  worktrees, your audit log.
- **People who want a Telegram / Feishu interface to coding and ops tasks**
  instead of another dashboard.

## Not for

- Public bots or shared workspaces.
- Multi-user teams — Conveyor has no concept of teams, roles, or tenancy.
- Untrusted users — `danger-full-access` is real, the allowlist is the only
  gate, and the bot will happily edit your repo.
- People who don't understand Codex permissions, worktrees, and `/diff`-before-
  `/apply` discipline.
- People looking for a SaaS product — there is no hosted Conveyor.
- Anyone who wants the bot to commit, push, or merge on its own. It won't.

---

## Like this?

If Conveyor matches your workflow, consider starring the repo. It helps other
self-hosted agent users find it, and keeps the project on my radar for the
next round of improvements.

---

## Reference: full command surface

The same commands work on Telegram and Feishu. Channel-specific behaviour is
documented in [`docs/architecture.en.md`](docs/architecture.en.md).

| Group | Commands |
|-------|----------|
| Codex jobs | `/run`, `/fix`, plain text → Codex in detached worktree |
| Job state | `/status`, `/last`, `/jobs [n]` |
| Worktree control | `/diff`, `/apply`, `/discard`, `/cancel` |
| Job queue | `/queue`, `/queue_cancel`, `/queue_cancel <id>`, `/queue_clear`, `/queue_pause`, `/queue_resume` |
| Memory | `/memo`, `记 <text>`, `/memory [date] [category]`, `/journal [n]` |
| Personal tools | `/note`, `/notes [query]`, `/remind`, `/reminders` |
| Scheduler | `/scheduler_status`, `/scheduler_probe`, `/scheduler_probe_live` |
| Briefing | `/brief_today`, `/brief_tomorrow`, `/brief_settings`, `/brief_enable`, `/brief_disable`, `/brief_probe` |
| Gmail | `/gmail_status`, `/gmail_recent [n]`, `/gmail_search <q>`, `/gmail_read <id>`, `/email_send` |
| Google OAuth | `/auth_google`, `/google_status`, `/google_revoke` |
| Calendar | `/calendar_today`, `/calendar_tomorrow`, `/calendar_week`, `/calendar_search`, `/calendar_freebusy`, `/calendar_create` |
| Contacts | `/contacts_search <q>` |
| GitHub | `/github_status`, `/github_issues`, `/github_issue <n>`, `/github_prs`, `/github_pr <n>`, `/github_ci`, `/github_create_issue`, `/github_comment` |
| Files / KB | `/files_roots`, `/files_search`, `/files_read`, `/kb_index`, `/kb_status`, `/kb_search`, `/project_docs` |
| Web | `/web_fetch`, `/web_text`, `/web_headers`, `/web_search`, `/research`, `/project_research` |
| Projects | `/projects`, `/project_add`, `/project_use`, `/project_show`, `/project_remove`, `/project_status`, `/project_health`, `/project_roadmap`, `/project_next`, `/project_release_checklist`, `/project_brief`, `/project_export`, `/project_export_all`, `/project_import`, `/project_template` |
| Planner | `/plan_today`, `/plan_dev`, `/planner_health` (alias `/project_health`), `/inbox_triage`, `/schedule_review`, `/planners` |
| Setup | `/setup`, `/setup_status`, `/setup_check`, `/setup_project`, `/setup_gmail`, `/setup_google`, `/setup_github` |
| Diagnostics | `/health [full] [json] [nosecurity]`, `/doctor`, `/diag [since]`, `/diagnose [server\|bot\|logs\|quick]`, `/audit [stale-min]`, `/security [since]`, `/ratelimit [n]`, `/audit_tools [n]` |
| Reports | `/metrics [n]`, `/log [sel]`, `/meta [sel]`, `/deploy_status` |
| Host ops | `/load`, `/vps`, `/htop`, `/ps`, `/disk`, `/logs`, `/service_status`, `/git_status` |
| Tools | `/tools`, `/nl_help` |
| Self-check | `/smoke`, `/editcheck` |
| Maintenance | `/maintain [keep]`, `/clean [keep]`, `/restart telegram\|feishu\|maintain` |
| Session | `/context`, `/forget` |
| **Execution nodes (P5.0 phase 0)** | `/nodes`, `/node_status`, `/computer_status` |
| **Computer Use direct mode (P5.6)** | `/computer_status`, `/computer_arm [min]`, `/computer_task <goal>`, `/computer_stop`, `/computer_log [task_id]`, `/computer_screenshot`, `/computer_observe`, `/computer_action <json>` (OFF by default) |
| Help | `/help` |

For danger levels (READ / WRITE_SAFE / WRITE / DESTRUCTIVE) and tool internals
see the `Agent tool layer` section in
[`docs/architecture.en.md`](docs/architecture.en.md).

---

## Reference: `.env` keys

The same `.env` works for both Telegram and Feishu. Leave the other channel's
fields empty if you only deploy one.

```dotenv
# --- Telegram (bot.py) ---
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_ALLOWED_USER_ID=123456789

# --- Feishu (feishu_bot.py) ---
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
LARK_ALLOWED_OPEN_ID=ou_xxx

# --- Codex (both channels) ---
CODEX_WORKSPACE_ROOT=/srv/my-repo
CODEX_BIN=/usr/local/bin/codex
CODEX_TASK_ROOT=/srv/conveyor

# LLM provider — at least one of OPENAI_API_KEY / MINIMAX_API_KEY
# must be set so Codex can authenticate.
# OPENAI_API_KEY=sk-replace_me
# MINIMAX_API_KEY=sk-replace_me
# MINIMAX_BASE_URL=https://api.minimaxi.com/v1

# --- Operator profile (onboarding) ---
# All four are optional; defaults in config.py apply.
# OPERATOR_NAME=
# OPERATOR_LANGUAGE=zh-CN
# OPERATOR_STYLE=terse
# OPERATOR_STANDING=personal-scale, single operator

# --- Optional tuning ---
# USER_TIMEZONE=America/Toronto
# TELEGRAM_PROGRESS_SECONDS=3
# CODEX_RETRY_429_DELAYS_SECONDS=300,900,1800
# CODEX_MODEL=
# CODEX_TIMEOUT_SECONDS=3600

# --- Optional: Execution Nodes (P5.0 phase 0) ---
# All four are optional. The desktop node is a stub and is offline
# regardless. See docs/desktop_agent_protocol.md and docs/desktop_security.md.
# CONVEYOR_DESKTOP_NODE_ENABLED=false
# CONVEYOR_DESKTOP_NODE_ID=macbook-payton
# CONVEYOR_DESKTOP_NODE_NAME=Payton MacBook
# CONVEYOR_DESKTOP_AGENT_TOKEN=replace_me_with_long_random_string
# CONVEYOR_COMPUTER_USE_DEFAULT_MODE=observe_only
```

`CODEX_WORKSPACE_ROOT` must be the top-level directory of a git repository.
The bot creates a detached worktree per day and writes job logs under
`CODEX_TASK_ROOT`. The full optional-surface (Gmail, Google OAuth, GitHub,
Briefing, Scheduler, Web Fetch / Search / Research, File Search / KB) is in
[`.env.example`](.env.example).

---

## Files

```text
conveyor/
  bot.py                  # Telegram command handlers (thin adapter)
  feishu_bot.py           # Feishu command handlers (thin adapter)
  config.py               # .env loading and validation
  runner.py               # shim → runner/ package
  redaction.py            # output redaction and truncation
  requirements.txt
  .env.example
  systemd/
    conveyor-telegram-bot.service
    conveyor-feishu-bot.service
    conveyor-maintain.service
    conveyor-maintain.timer
    conveyor-scheduler.service
    conveyor-scheduler.timer
  channel/
    types.py              # InboundMessage, OutboundPort
    auth.py               # is_allowed per channel
    feishu_cards.py       # Feishu interactive message card builders
  handlers/
    dispatch.py           # single entry: auth → command/memo/codex
    commands.py           # COMMAND_TABLE
    memo.py               # "记 x" / /memo fast path
    jobs.py               # /run, /fix, free-text → CodexRunner
    intent.py             # route_intent (deterministic | hybrid | llm)
    nl_router.py          # natural-language catalog and routing
    tools/                # agent tool layer: registry, executors, audit
  nodes/                  # execution nodes (VPS + desktop stub, P5.0)
  personal_tools/         # notes, reminders, gmail, google, github, …
  scripts/                # CLI tools, harnesses, smokes
  Makefile
  README.md
  docs/
    architecture.md       # 设计 (中文)
    architecture.en.md    # Architecture & design (English)
    desktop_agent_protocol.md  # future local desktop agent (P5.x)
    desktop_security.md   # desktop / Computer Use safety contract
```

`docs/` is the only documentation shipped in the repo. Anything not in
`docs/` and `README.md` lives in the maintainer's personal notes — by
design, this repo is intended to be a small, focused personal tool.

---

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
make smoke           # env-free AST/behavior smokes, <30s, pre-deploy gate
make smoke-all       # also runs scripts that need a populated .env
```

`make smoke` is the pre-deploy gate. PRs that break it will not be merged.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the commit and PR style. The
included `scripts/docs_consistency_smoke.py` validates that READMEs, arch
docs, runtime sandbox mode, and active service naming stay aligned.

For design notes — runtime layout, channel types, the agent tool layer,
command table, harness matrix, and backlog — see
[`docs/architecture.en.md`](docs/architecture.en.md) (or the Chinese version:
[`docs/architecture.md`](docs/architecture.md)).

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Bot does not respond on Telegram | `journalctl -u conveyor-telegram-bot`; verify `TELEGRAM_ALLOWED_USER_ID` matches your user id |
| Feishu: no reply to DM | `im:message.p2p_msg:readonly` enabled AND new version published; `journalctl -u conveyor-feishu-bot` for `400` from open.feishu.cn |
| Feishu: `Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]` | `im:message:send_as_bot` not granted, or app version not published, or app not installed to enterprise |
| Feishu: `400` from `/contact/v3/users/batch` on every message | `contact:user.id:readonly` not granted; harmless but noisy. Add the scope, publish a new version |
| Feishu: WebSocket immediate disconnect | `.env` value has stray whitespace, quotes, or CJK punctuation. Re-edit with `nano` |
| Long-connection save fails | The local `feishu_bot.py` must be running before you save the event subscription |
| Job stuck in `running` | `/cancel` or `sudo systemctl restart conveyor-telegram-bot`; check for repeated `Reconnecting... high demand` in `journalctl` |
| Telegram replies are very slow | `TELEGRAM_PROGRESS_SECONDS` (default 3s) controls placeholder edits; rate limit is 20 edits/min on Telegram |

### Live Telegram smoke (manual, optional)

`scripts/telegram_live_smoke.py` drives the bot as a **real Telegram user**
(Telethon) and checks the agent tool layer end-to-end. It is the only way to
exercise the bot's `MessageHandler` because Telegram Bot API messages do not
trigger the bot's own handlers when sent by the bot itself. It is **not** part
of `make smoke`; install Telethon explicitly when you want to run it:

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

Restart confirmation is **cancelled by default**. To actually restart a
conveyor service, both gates must be open:

```bash
TELEGRAM_LIVE_ALLOW_RESTART=1 \
  .venv/bin/python scripts/telegram_live_smoke.py --full --allow-restart
```

The script never prints bot tokens, api hash, session paths, or `.env`
content; `.telegram-live-smoke*` is git-ignored.

---

## Automatic VPS deploy (GitHub Actions)

When you push to `main`, GitHub Actions SSHs into the VPS, pulls the latest
code, runs smoke tests, and restarts services — all in one step. Smoke
failure prevents the restart.

### Required GitHub secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `VPS_HOST` | yes | VPS hostname or IP |
| `VPS_USER` | yes | SSH user (prefer a dedicated deploy user) |
| `VPS_SSH_KEY` | yes | Private key for the deploy user |
| `VPS_PORT` | no | SSH port (default 22) |
| `CONVEYOR_DEPLOY_PATH` | no | Repo root on VPS (default `/opt/conveyor`) |

### VPS one-time setup

1. Clone the repo at `/opt/conveyor`:

   ```bash
   sudo mkdir -p /opt/conveyor
   sudo chown $USER /opt/conveyor
   git clone https://github.com/mammut001/Conveyor.git /opt/conveyor
   ```

2. Create `.env` on the VPS (never commit it).
3. Create `.venv` and install the initial dependencies:

   ```bash
   cd /opt/conveyor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

   Each deploy re-syncs `.venv` from `requirements.txt` before running smoke tests.

4. Install systemd services:

   ```bash
   sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
   sudo cp systemd/conveyor-feishu-bot.service   /etc/systemd/system/
   sudo cp systemd/conveyor-maintain.service     /etc/systemd/system/
   sudo cp systemd/conveyor-maintain.timer       /etc/systemd/system/
   sudo cp systemd/conveyor-scheduler.service    /etc/systemd/system/
   sudo cp systemd/conveyor-scheduler.timer      /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now conveyor-telegram-bot conveyor-feishu-bot
   sudo systemctl enable --now conveyor-maintain.timer conveyor-scheduler.timer
   ```

5. Grant the deploy user passwordless sudo for exactly these commands:

   ```
   # /etc/sudoers.d/conveyor-deploy
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart conveyor-feishu-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl status  conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl status  conveyor-feishu-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active conveyor-telegram-bot
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active conveyor-feishu-bot
   ```

### Manual deploy test

```bash
ssh user@host 'bash /opt/conveyor/scripts/deploy_vps.sh'
```

### Manual VPS Update (v0.1.1 Upgrade)

To upgrade your VPS manually and apply the v0.1.1 security hardening updates:

```bash
cd /opt/conveyor
git pull
make smoke
sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
sudo cp systemd/conveyor-maintain.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart conveyor-telegram-bot
sudo systemctl restart conveyor-maintain.timer
python scripts/security_audit.py --env /opt/conveyor/.env --service conveyor-telegram-bot --since "24 hours ago"
```

### How it works

1. GitHub Actions triggers on every push to `main` (or manual dispatch).
2. It SSHs into the VPS and runs `scripts/deploy_vps.sh`.
3. The script:
   - acquires a `flock` lock (no concurrent deploys)
   - `git fetch origin main && git reset --hard origin/main`
   - backs up key files before reset
   - syncs `.venv` from `requirements.txt`
   - runs `make smoke`
   - if smoke passes: restarts `conveyor-telegram-bot` + `conveyor-feishu-bot`
   - if smoke fails: exits nonzero, services are NOT restarted
   - writes `.deploy-status.json` with deploy metadata
   - if restart health check fails: attempts rollback from backup
4. `.env` is never printed or committed.

There is also an rsync-based deploy (`scripts/deploy.sh`) for local use that
rsyncs source files then runs the same remote smoke + restart flow.

### `/deploy_status` command

Send `/deploy_status` to the bot to see last deploy time, source, git SHA,
smoke result and service states, current runtime git SHA, branch, progress
mode, and live `systemctl is-active` for both services.

### Limitations

- The live Telegram smoke (`scripts/telegram_live_smoke.py`) is NOT run
  automatically — it needs real Telegram credentials and is manual-only.
- The deploy script assumes `.venv` already exists on the VPS, then keeps
  its packages synced from `requirements.txt` on each deploy. If you need to
  bootstrap a fresh VPS, run `scripts/install-remote.sh` first.
- Rollback is minimal: key files are backed up before reset, and if services
  fail to start after restart, the script restores from backup and retries.
  This does not cover all failure modes.

---

## License

MIT — see [`LICENSE`](LICENSE).
