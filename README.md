# Conveyor

A small personal transport that bridges one whitelisted user
(Telegram and/or Feishu) to a [`codex`](https://github.com/openai/codex)
CLI running on a VPS. Type a message from your phone, get a Codex
answer back; same for `记 xxx` (write to memory), `/status`, `/diff`,
`/run` — all the commands. Single operator, single VPS, no SaaS.

> Conveyor is the **transport** layer, not the agent. Codex CLI is
> the agent. See [`docs/architecture.md`](docs/architecture.md) for
> the full design.
>
> **[中文文档 / Chinese](README.zh.md)**

---

## 1. Quick Start

VPS prerequisites: Ubuntu, [`codex` CLI](https://github.com/openai/codex)
installed and authenticated for the SSH user, and a git repository
for `CODEX_WORKSPACE_ROOT`.

**First install** (run from a laptop with SSH access; the host and
secrets stay in your shell environment, never in the repo):

```bash
git clone https://github.com/mammut001/conveyor.git
cd conveyor
CONVEYOR_REMOTE=ubuntu@<host> bash scripts/install-remote.sh
```

The installer rsyncs source, creates `.venv`, installs systemd units
(`conveyor-telegram-bot`, `conveyor-feishu-bot`, `conveyor-maintain`),
runs the interactive `configure_env.py` helper when `.env` is missing,
and starts the services. Open Telegram and send `/start` when done.

**Code updates** (after first install):

```bash
CONVEYOR_REMOTE=ubuntu@<host> bash scripts/deploy.sh
```

Optional local shell shortcut (`~/.zshrc`):

```bash
export CONVEYOR_REMOTE=ubuntu@<host>
export CONVEYOR_REMOTE_DIR=/opt/conveyor
alias deploy-runner='cd ~/conveyor && bash scripts/deploy.sh'
```

---

## 2. Feishu bot — full setup

Same `conveyor` install as above; just configure Feishu as a second
channel.

### 2.1 Get credentials

In the [飞书开放平台](https://open.feishu.cn/app) developer
console, create an app, then:

- **凭证与基础信息** → copy `App ID` and `App Secret`

These go into `LARK_APP_ID` and `LARK_APP_SECRET` in `.env` (see §2.5).

### 2.2 Enable the 机器人 capability

**应用能力 → 机器人 → 启用** (off by default — without it the
`im.message.receive_v1` event will not appear in the event list).

### 2.3 Open the scopes you need

**权限管理** — search and add:

| Search keyword | Scope | Why |
|----------------|-------|-----|
| `p2p` | `im:message.p2p_msg:readonly` | Receive DMs |
| `send_as_bot` | `im:message:send_as_bot` | Send messages as the bot |
| `group_at` | `im:message.group_at_msg:readonly` | Group @-mention (optional) |
| `user.id` | `contact:user.id:readonly` | Sender info resolution (recommended) |

### 2.4 Subscribe to the event

- **事件订阅** → Subscription method = **长连接 / persistent
  connection**
- The local bot must be **already running** before you can save the
  subscription
- Add event: `im.message.receive_v1` (**Receive message**), under
  **应用身份** (not 机器人身份 — that's a different long-connection
  type)

### 2.5 Configure `.env`

Edit `/opt/conveyor/.env` and add (placeholders, never commit real
values):

```dotenv
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=replace_me
# LARK_ALLOWED_OPEN_ID=ou_xxx
```

The Feishu bot runs in **bootstrap mode** until `LARK_ALLOWED_OPEN_ID`
is set: it will accept any sender, reply with the sender's `open_id`,
and ask you to put that into `.env` then restart. This is a one-time
handshake so you don't have to fish the open_id out of the log.

After you write the open_id:

```bash
sudo systemctl restart conveyor-feishu-bot
sudo journalctl -u conveyor-feishu-bot -f
```

You should see `connected to wss://msg-frontier.feishu.cn/ws/v2...`.

### 2.6 Publish and install

**版本管理与发布** → create a new version → 审核 (usually instant
for internal apps) → **申请发布** → **安装到企业**. Scope changes
do NOT take effect on the live app until a new version is published.

### 2.7 Full setup checklist

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

---

## 3. `.env` reference

The same `.env` file works for both channels. Telegram-only and
Feishu-only deploys just leave the other channel's fields empty.

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
```

`CODEX_WORKSPACE_ROOT` must be the top-level directory of a git
repository. The bot creates a detached worktree per day and writes
job logs under `CODEX_TASK_ROOT`.

`chmod 600 .env` after editing. The systemd unit reads it via
`EnvironmentFile=` and never echoes values.

---

## 4. Commands (Telegram + Feishu, same surface)

- Plain text → run Codex with full workspace access (same as `/run`)
- `/run <prompt>` and `/fix <prompt>` are equivalent; both use
  `danger-full-access`
- `/status` / `/last` / `/jobs [n]` — current/most recent jobs
- `/diff` — `git status` + truncated diff preview from the latest worktree
- `/apply` — apply the latest worktree back to the main repo
  (only when main is clean)
- `/discard` — drop the latest worktree
- `/cancel` — terminate the running Codex process
- `/clean [keep]` / `/maintain [keep]` — cleanup old jobs and worktrees
- `/health [full] [json] [nosecurity]` — compact health snapshot
- `/doctor` / `/diag [since]` — full backend checks, one-line triage
- `/audit [stale-min]` / `/security [since]` / `/ratelimit [n]` —
  audit and report harnesses
- `/metrics [n]` / `/log [sel]` / `/meta [sel]` — recent trends,
  log summary, job.json sidecar
- `/smoke` / `/editcheck` — end-to-end and real-edit self-checks
- `/memo <text>` / `记 <text>` — write to today's `MEMORY.md` (no Codex)
- `/memory [date] [category]` / `/journal [n]` — read MEMORY.md
  and archived journals
- `/note <text>` — save a local note (**WRITE**, confirm)
- `/notes [query]` — list recent notes or search
- `/remind <text + time>` — create a local reminder (**WRITE**, confirm)
- `/reminders` — list reminders
- `/help` — full command list

### Agent tool layer

Conveyor is **not** only a hardcoded command bot. A structured tool
registry plus a lightweight intent router sit between chat input and
Codex:

| Path | When | Example |
|---|---|---|
| **Deterministic** | Stable host checks | `看看磁盘`, `/logs`, `git status` |
| **Hybrid** | Diagnosis / “why” questions | `/diagnose server`, `为什么服务器这么慢` → collect facts, then Codex analyzes |

**Explicit hybrid diagnose:** `/diagnose [server|bot|logs|quick]` (default
`server`) runs a mode-specific tool bundle, then Codex analysis in Chinese
with likely cause, severity, and safe next steps. This is separate from
`/diag` (job/runtime diagnostics harness).

**Friendly restart aliases:** `/restart telegram|feishu|maintain` maps to
whitelist systemd units and uses the same confirmation flow as
`service_restart` (inline button or explicit `确认执行`).

**Confirmation binding:** Pending dangerous actions are scoped to
`operator_id + chat_id + channel`; confirming from another chat is rejected.

**Audit log:** WRITE/DESTRUCTIVE tool events append JSONL to
`codex_memory_root/audit/tools.log`. `/audit_tools [n]` shows recent
redacted entries (READ only).
| **LLM** | Open-ended coding / debugging | `写个 quicksort`, `fix this test` |

Registered tools (`/tools` lists all):

| Tool | Danger | What it does |
|---|---|---|
| `load` | read | Host load / memory / CPU / top processes |
| `ps` | read | Top processes (`comm` mode by default) |
| `htop` | read | Non-interactive top frame |
| `disk` | read | `df` for `/`, `/srv`, `/opt` |
| `logs` | read | `journalctl` tail for conveyor services |
| `service_status` | read | `systemctl is-active` for conveyor units |
| `git_status` | read | Workspace `git status` |
| `service_restart` | **write (confirm)** | Restart a conveyor systemd unit |

Safety: **write/destructive tools require explicit confirmation**
(Telegram inline buttons; Feishu/text replies must use explicit phrases
like `确认执行` / `confirm` — casual `好` / `ok` / `是` is intentionally
**not** accepted). Confirmations are bound to the originating chat and
channel. Events are audit-logged under `audit/tools.log`.

Implementation: `handlers/tools/` (registry + executors + runner),
`handlers/intent.py` (`route_intent`). Handlers stay channel-agnostic;
Telegram callbacks use `tool:confirm:<token>` / `tool:cancel:<token>`.

### Personal Tools Hub (P3.1 — local only)

Structured foundation for future Gmail / Calendar / Contacts / GitHub
integrations. **OAuth tokens never enter Codex prompts** — only
server-side executors run on the VPS.

| Storage | `$CODEX_MEMORY_ROOT/personal_tools.db` (SQLite) |
|---|---|
| Notes | `/note`, `/notes` → `notes.add/search/list_recent/delete` |
| Reminders | `/remind`, `/reminders` → `reminders.create/list/cancel/due` |

**Danger levels and UX choice:**

| Tool | Level | Confirmation? | Rationale |
|---|---|---|---|
| `notes.add` | WRITE_SAFE | No | append-only, low-risk, reversible via `notes.delete` |
| `reminders.create` | WRITE_SAFE | No | same; confirmation would break `/remind in 10m X` fluency |
| `notes.delete` | DESTRUCTIVE | Yes | destructive — deletes data |
| `reminders.cancel` | WRITE | Yes | mutates status — needs intent check |
| `notes.search` / `list_recent` / `reminders.list` / `due` | READ | No | read-only |

`WRITE_SAFE` = executes immediately, args + result preview audit-logged
with redaction to `audit/tools.log`. No interactive confirmation required
because these are personal append/create operations; the operator can
always delete or cancel afterwards.

Reminder time parsing (phase P3.1): `in 10m`, `in 2h`, `tomorrow HH:MM`,
ISO datetime. Parse failures return usage text. `notes.delete` and
`reminders.cancel` reuse the same confirmation + `audit/tools.log` redaction
as host tools.

Code: `personal_tools/` (`base`, `store`, `registry`, `notes`, `reminders`).
Smoke: `scripts/personal_tools_smoke.py` (16 cases: CRUD, isolation, audit,
redaction, command surface).

**Telegram slash commands:** New ops/tool commands (`/load`, `/tools`,
`/disk`, …) are registered in `COMMAND_TABLE` and reached via a
generic `MessageHandler(filters.COMMAND, …)` fallback in `bot.py` (after
explicit `CommandHandler`s, before plain text). This ensures unknown
slash commands still route through `dispatch()` → `COMMAND_TABLE`.

### Deterministic host ops (legacy slash commands)

These slash commands and matching natural-language phrases still work
and map into the tool layer above:

| Command | Phrasing | What it does |
|---|---|---|
| `/load` (alias `/vps`) | `看看我的负载`, `check vps load` | Hostname, time, uptime, CPU count, memory, disk for `/ /srv /opt`, top CPU/mem processes. |
| `/htop` | `跑一下 htop`, `top 看一下` | htop is a TUI; returns a `top -bn1` snapshot with a one-line TUI explanation. Intent matching is conservative — coding/docs requests mentioning htop (e.g. “look at htop source code”) route to LLM, not ops. |
| `/ps` | `ps aux`, `哪些进程` | Top processes by CPU/mem. Default uses `comm` only (no argv → no token leak). `/ps full` shows a safety warning; `/ps full confirm` includes args (still redacted/truncated). |

Safety:

- Uses argument arrays (no shell interpolation of user text).
- 5-second timeout per subprocess.
- `comm` not `args` for `/ps` default, so secrets in argv are not
  exposed.
- Output is run through `redact_text` and `truncate`.
- Never reads environment variables, `.env`, or full process command
  lines by default.

The bot runs on a single VPS, so the snapshot is for that one
machine. The reply explicitly says "this is the machine where the bot
service is running" so the operator does not confuse it with the
`codex exec` sandbox view.

Telegram shows the ops output as a fresh message (no streaming
edit). For long-running Codex jobs, Telegram edits the original
"⏳ 收到，处理中..." placeholder in place; Feishu currently
degrades to fresh messages (cards / streaming are P2.2 backlog).

Only one Codex job runs at a time. Replies are intentionally quiet:
start acknowledgement, useful retry/failure notices, final answer.
Raw JSONL events stay on disk under `logs/<job-id>/`.

`CODEX_RETRY_429_DELAYS_SECONDS` controls the backoff schedule for
transient `429 Too Many Requests` from the provider.

---

## 5. Safety model

This is a **single-operator private VPS control surface**, not a
multi-tenant SaaS. There is one whitelisted chat per channel and one
human who reviews diffs before anything lands in the main repo.

- Channel access is denied unless the sender id exactly matches
  `TELEGRAM_ALLOWED_USER_ID` or `LARK_ALLOWED_OPEN_ID`. There is no
  other authentication; the ALLOWED_* gate is the only thing
  standing between this bot and the public internet.
- Prompts are passed only to Codex stdin; they are never executed
  as shell commands.
- `/run`, plain text, and `/fix` all invoke Codex with
  `danger-full-access` in the daily worktree (chat-first; see
  `docs/architecture.en.md` §5). This is intentional for a personal
  VPS: shell, host reads, and worktree edits must work from chat.
- Each job uses a detached git worktree created from `HEAD`.
- Raw Codex JSONL stays on disk; Telegram / Feishu output is
  truncated and redacted for common secret patterns.
- The systemd units set `PYTHONDONTWRITEBYTECODE=1` so runtime
  imports do not leave `__pycache__` files in the deployed
  directory.
- The bot does **not** commit, push, or merge changes. Apply is
  always an explicit `/apply` after you review `/diff`.

**Operational boundaries today:** channel allowlist, low-privilege VPS
user, per-day worktree isolation, output redaction, and your review
discipline (`/diff` then `/apply`). **Future hardening** (e.g. narrowing
sandbox back toward `workspace-write`) is backlog — not current behavior.

This is still remote code-running infrastructure: keep the bot
tokens private, use dedicated bots, keep the VPS user low privilege,
and review `/diff` before manually applying or copying changes.

---

## 6. Files

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
    conveyor.env.example
  channel/
    types.py              # InboundMessage, OutboundPort
    auth.py               # is_allowed per channel
  handlers/
    dispatch.py           # single entry: auth → command/memo/codex
    commands.py           # 23-command COMMAND_TABLE
    memo.py               # "记 x" / /memo fast path
    jobs.py               # /run, /fix, free-text → CodexRunner
  scripts/                # CLI tools, harnesses, smokes
  Makefile
  README.md
  docs/
    architecture.md       # design (Conveyor vs Hermes, channel
                          # decoupling, phase progress)
```

`docs/` is the only documentation shipped in the repo. Anything that
isn't in `docs/` and `README.md` (architecture, design notes, design
diaries) lives in the maintainer's personal notes — by design, this
repo is intended to be a small, focused personal tool.

---

## 7. Local development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
make smoke           # env-free AST/behavior smokes, <30s, pre-deploy gate
make smoke-all       # also runs scripts that need a populated .env
```

`make smoke` is the pre-deploy gate. PRs that break it will not be
merged. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the commit and
PR style.

For design notes — runtime layout, channel types, the agent tool
layer, command table, harness matrix, and backlog — see
[`docs/architecture.en.md`](docs/architecture.en.md) (or the Chinese
version: [`docs/architecture.md`](docs/architecture.md)).

---

## 8. Troubleshooting

| Symptom | Check |
|---------|-------|
| Bot does not respond on Telegram | `journalctl -u conveyor-telegram-bot`; verify `TELEGRAM_ALLOWED_USER_ID` matches your user id |
| Feishu: no reply to DM | `im:message.p2p_msg:readonly` enabled AND new version published; `journalctl -u conveyor-feishu-bot` for `400` from open.feishu.cn |
| Feishu: `Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]` | `im:message:send_as_bot` not granted, or app version not published, or app not installed to enterprise |
| Feishu: `400` from `/contact/v3/users/batch` on every message | `contact:user.id:readonly` not granted; harmless but noisy. Add the scope, publish a new version |
| Feishu: WebSocket immediate disconnect | `.env` value has stray whitespace, quotes, or CJK punctuation. Re-edit with `nano` |
| Long-connection save fails | The local `feishu_bot.py` must be running before you save the event subscription |
| Job stuck in `running` | `/cancel` or `sudo systemctl restart conveyor-telegram-bot`; check for repeated `Reconnecting... high demand` in `journalctl` |

## Live Telegram smoke (manual, optional)

`scripts/telegram_live_smoke.py` drives the bot as a **real Telegram
user** (Telethon) and checks the agent tool layer end-to-end. This is
the only way to exercise the bot's `MessageHandler` because Telegram
Bot API messages do not trigger the bot's own handlers when sent by
the bot itself.

It is **not** part of `make smoke`. Install Telethon explicitly when
you want to run it:

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

Restart confirmation is **cancelled by default**. To actually
restart a conveyor service, both gates must be open:

```bash
TELEGRAM_LIVE_ALLOW_RESTART=1 \
  .venv/bin/python scripts/telegram_live_smoke.py --full --allow-restart
```

The script never prints bot tokens, api hash, session paths, or
`.env` content; `.telegram-live-smoke*` is git-ignored.
| Telegram replies are very slow | `TEGRAM_PROGRESS_SECONDS` (default 3s) controls placeholder edits; rate limit is 20 edits/min on Telegram |

---

## Automatic VPS deploy (GitHub Actions)

When you push to `main`, GitHub Actions SSHs into the VPS, pulls the
latest code, runs smoke tests, and restarts services — all in one
step. Smoke failure prevents the restart.

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
3. Create `.venv` and install dependencies:
   ```bash
   cd /opt/conveyor
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
4. Install systemd services:
   ```bash
   sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
   sudo cp systemd/conveyor-feishu-bot.service   /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable conveyor-telegram-bot conveyor-feishu-bot
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

### How it works

1. GitHub Actions triggers on every push to `main` (or manual dispatch).
2. It SSHs into the VPS and runs `scripts/deploy_vps.sh`.
3. The script:
   - acquires a `flock` lock (no concurrent deploys)
   - `git fetch origin main && git reset --hard origin/main`
   - backs up key files before reset
   - runs `make smoke`
   - if smoke passes: restarts `conveyor-telegram-bot` + `conveyor-feishu-bot`
   - if smoke fails: exits nonzero, services are NOT restarted
   - writes `.deploy-status.json` with deploy metadata
   - if restart health check fails: attempts rollback from backup
4. `.env` is never printed or committed.

There is also an rsync-based deploy (`scripts/deploy.sh`) for local
use that rsyncs source files then runs the same remote smoke + restart
flow.

### `/deploy_status` command

Send `/deploy_status` to the bot to see:
- last deploy time, source, git SHA
- smoke result and service states (from `.deploy-status.json`)
- current runtime git SHA, branch, progress mode
- live `systemctl is-active` for both services

### Limitations

- The live Telegram smoke (`scripts/telegram_live_smoke.py`) is NOT
  run automatically — it needs real Telegram credentials and is
  manual-only.
- The deploy script assumes `.venv` already exists on the VPS. If
  you need to bootstrap a fresh VPS, run `scripts/install-remote.sh`
  first.
- Rollback is minimal: key files are backed up before reset, and if
  services fail to start after restart, the script restores from
  backup and retries. This does not cover all failure modes.

---

## 9. License

MIT — see [`LICENSE`](LICENSE).
