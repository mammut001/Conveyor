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
  — single-concurrency FIFO for Codex jobs.

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
  handlers/
    dispatch.py           # single entry: auth → command/memo/codex
    commands.py           # COMMAND_TABLE
    memo.py               # "记 x" / /memo fast path
    jobs.py               # /run, /fix, free-text → CodexRunner
    intent.py             # route_intent (deterministic | hybrid | llm)
    nl_router.py          # natural-language catalog and routing
    tools/                # agent tool layer: registry, executors, audit
  personal_tools/         # notes, reminders, gmail, google, github, …
  scripts/                # CLI tools, harnesses, smokes
  Makefile
  README.md
  docs/
    architecture.md       # 设计 (中文)
    architecture.en.md    # Architecture & design (English)
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