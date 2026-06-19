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

## 1. Quick Start (10 min)

**Prerequisites:** Ubuntu VPS, SSH access, [`codex` CLI](https://github.com/openai/codex) installed.

### 1.1 Install (from your laptop)

```bash
git clone https://github.com/mammut001/conveyor.git && cd conveyor
sudo bash scripts/install.sh
```

The installer will:
1. Install system dependencies
2. Sync code to `/opt/conveyor`
3. Create Python `.venv`
4. Prompt for `.env` configuration
5. Install and start systemd services

### 1.2 Configure `.env`

Edit `/opt/conveyor/.env` (see [`.env.example`](.env.example) for all options):

```bash
sudo nano /opt/conveyor/.env
```

**Minimum required:**
```dotenv
TELEGRAM_BOT_TOKEN=123456789:from_botfather
TELEGRAM_ALLOWED_USER_ID=your_user_id
CODEX_WORKSPACE_ROOT=/path/to/your/repo
```

### 1.3 Restart and test

```bash
sudo systemctl restart conveyor-telegram-bot
sudo systemctl status conveyor-telegram-bot
```

Open Telegram, send `/start` to your bot. Done!

### 1.4 Update (later)

```bash
cd conveyor && git pull
sudo bash scripts/install.sh --update
```

### 1.5 Optional: Feishu bot

See [§2. Feishu bot setup](#2-feishu-bot--full-setup) for adding a second channel.

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
- `/note <text>` — save a local note (**WRITE_SAFE**, audited, no confirm)
- `/notes [query]` — list recent notes or search
- `/remind <text + time>` — create a local reminder (**WRITE_SAFE**, audited, no confirm)
- `/reminders` — list reminders
- `/scheduler_status` — reminder scheduler status report
- `/scheduler_probe` — dry-run probe (no network, no DB writes)
- `/scheduler_probe_live` — real delivery test (**WRITE**, requires confirmation)
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
| `scheduler_status` | read | Reminder scheduler status report |
| `scheduler_probe` | read | Scheduler dry-run probe |
| `scheduler_probe_live` | **write (confirm)** | Scheduler live delivery test |

Safety: **write/destructive tools require explicit confirmation**
(Telegram inline buttons; Feishu/text replies must use explicit phrases
like `确认执行` / `confirm` — casual `好` / `ok` / `是` is intentionally
**not** accepted). Confirmations are bound to the originating chat and
channel. Events are audit-logged under `audit/tools.log`.

Implementation: `handlers/tools/` (registry + executors + runner),
`handlers/intent.py` (`route_intent`). Handlers stay channel-agnostic;
Telegram callbacks use `tool:confirm:<token>` / `tool:cancel:<token>`.

### Personal Tools Hub (P3.1 + P3.2 — local notes/reminders + delivery)

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

Reminder time parsing: `in 10m`, `in 2h`, `tomorrow HH:MM`,
ISO datetime. Parse failures return usage text. `notes.delete` and
`reminders.cancel` reuse the same confirmation + `audit/tools.log` redaction
as host tools.

**P3.2 — Reminder delivery:** When `/remind` creates a reminder, the bot
stores `msg.channel` (telegram/feishu) and `msg.chat_id`. A systemd timer
(`conveyor-scheduler.timer`) runs `scripts/scheduler_tick.py` every 60s
to find due reminders and deliver them as Telegram messages. Delivery
status is tracked per-reminder (`pending` → `delivered`/`failed`); failed
reminders retry up to 3 times. Reminders without `channel`/`chat_id`
(pre-P3.2 records) are skipped by the scheduler with a clear status.

**P3.2.1 — Scheduler observability:** Three tools let the operator
verify the delivery pipeline from chat without SSH:

| Tool | Level | Command | What it does |
|---|---|---|---|
| `scheduler_status` | READ | `/scheduler_status` | Timer/Service status, journal tail, reminder counts, channel support |
| `scheduler_probe` | READ | `/scheduler_probe` | Dry-run probe: runs scheduler_tick --dry-run, no network/DB writes |
| `scheduler_probe_live` | WRITE | `/scheduler_probe_live` | Live delivery test to Telegram (requires confirmation) |

`scheduler_status_report()` degrades gracefully when `systemctl` is
unavailable (macOS/CI). `scheduler_probe_live()` creates a `[probe]`
reminder, delivers it, and verifies `delivery_status=delivered` in DB.
All output is redacted; no `.env` or tokens are exposed.

Code: `personal_tools/` (`base`, `store`, `registry`, `notes`, `reminders`,
`reminder_parse`). Scheduler: `scripts/scheduler_tick.py`.
Probe: `scripts/scheduler_probe.py`.
Smoke: `scripts/personal_tools_smoke.py` (24 cases: CRUD, isolation, audit,
redaction, command surface, migration, delivery, dry-run, drift check).
Smoke: `scripts/scheduler_probe_smoke.py` (7 cases: registry, commands,
no-systemctl degradation, dry-run, live confirmation, /tools, /help).

**P3.3 — Gmail App Password MVP:** Conservative Gmail integration using
IMAP + SMTP with Gmail App Password. **OAuth is a future phase.**

| Tool | Level | Command | What it does |
|---|---|---|---|
| `gmail.status` | READ | `/gmail_status` | Gmail connection status |
| `gmail.recent` | READ | `/gmail_recent [n]` | Recent emails from INBOX |
| `gmail.search` | READ | `/gmail_search <query>` | Search emails by subject/from |
| `gmail.read` | READ | `/gmail_read <id>` | Read specific email |
| `email.send` | WRITE | `/email_send <to> \| <subject> \| <body>` | Send email (requires confirmation) |

Environment variables:

| Variable | Required | Default |
|---|---|---|
| `GMAIL_BACKEND` | Yes | `imap_smtp` |
| `GMAIL_ADDRESS` | Yes | — |
| `GMAIL_APP_PASSWORD` | Yes | — (16-char App Password) |
| `GMAIL_IMAP_HOST` | No | `imap.gmail.com` |
| `GMAIL_IMAP_PORT` | No | `993` |
| `GMAIL_SMTP_HOST` | No | `smtp.gmail.com` |
| `GMAIL_SMTP_PORT` | No | `587` |

Security: App password is **never** exposed in chat replies, logs, audit
logs, or `repr()`. Sending requires WRITE confirmation. No delete/archive
label operations in this phase. Attachments are not downloaded.

Natural language intent: `帮我看一下收件箱` → `gmail.recent`,
`邮箱状态` → `gmail.status`, `搜索邮件 关于发票` → `gmail.search`,
`发邮件` → prompts for details.

Smoke: `scripts/gmail_smoke.py` (9 cases: config, registry, commands,
missing config, no network, parse errors, confirmation, redaction, help/tools).

**P3.4 — Google Calendar + Contacts:** Google OAuth broker with
read-first Calendar and Contacts tools. **Gmail remains App Password
backend; OAuth only for Calendar/Contacts.**

| Tool | Level | Command | What it does |
|---|---|---|---|
| `google.status` | READ | `/google_status` | OAuth token status |
| `google.auth` | WRITE | `/auth_google` | Start OAuth flow |
| `google.revoke` | DESTRUCTIVE | `/google_revoke` | Revoke and delete token |
| `calendar.status` | READ | `/calendar_status` | Calendar connection status |
| `calendar.today` | READ | `/calendar_today` | Today's events |
| `calendar.tomorrow` | READ | `/calendar_tomorrow` | Tomorrow's events |
| `calendar.week` | READ | `/calendar_week` | This week's events |
| `calendar.search` | READ | `/calendar_search <query>` | Search events |
| `calendar.freebusy` | READ | `/calendar_freebusy <range>` | Check free/busy |
| `calendar.create` | WRITE | `/calendar_create <title> \| <time> \| <desc>` | Create event (confirm) |
| `contacts.search` | READ | `/contacts_search <query>` | Search contacts |

Environment variables:

| Variable | Required | Default |
|---|---|---|
| `GOOGLE_CLIENT_SECRET_PATH` | Yes | — (path to client_secret JSON) |
| `GOOGLE_TOKEN_PATH` | No | `codex_memory_root/secrets/google_token.json` |
| `GOOGLE_OAUTH_SCOPES` | No | calendar + contacts.readonly |
| `GOOGLE_OAUTH_REDIRECT_PORT` | No | `8765` |

Security: OAuth tokens stored at `secrets/google_token.json` with chmod 600.
Tokens never appear in chat, logs, audit, or `repr()`. All API errors
are redacted.

Natural language intent: `看看今天的日程` → `calendar.today`,
`搜索日程 关于会议` → `calendar.search`, `找一下联系人 张三` → `contacts.search`.

Dependencies: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`.

Smoke: `scripts/google_tools_smoke.py` (10 cases: missing config, missing
auth, setup instructions, confirmation, token path, registry, commands,
help/tools, intent routing).

**P3.5 — Daily Briefing:** Daily briefing system aggregating Calendar,
reminders, Gmail, and notes.

| Tool | Level | Command | What it does |
|---|---|---|---|
| `briefing.status` | READ | `/brief_settings` | Briefing settings status |
| `briefing.today` | READ | `/brief_today` | Today's briefing |
| `briefing.tomorrow` | READ | `/brief_tomorrow` | Tomorrow's briefing |
| `briefing.enable` | WRITE_SAFE | `/brief_enable [HH:MM]` | Enable daily briefing |
| `briefing.disable` | WRITE | `/brief_disable` | Disable daily briefing (requires confirmation) |
| `briefing.probe` | READ | `/brief_probe` | Briefing probe (dry-run) |

Briefing content includes: calendar events (requires Google OAuth), due
reminders, recent email summary (requires Gmail), recent notes. Missing
providers are gracefully degraded.

Scheduler integration: `scripts/scheduler_tick.py` checks enabled briefing
settings every minute, sends briefings when local time is reached, and
avoids duplicate sends.

Storage: `briefing_settings` and `briefing_runs` tables in `personal_tools.db`.

Security: No raw email bodies, no OAuth tokens, no passwords. Output
processed through `redact_text()` + `truncate()`.

Natural language: `今日简报` → `briefing.today`, `启用每日简报` → `briefing.enable`,
`禁用简报` → `briefing.disable`.

Smoke: `scripts/briefing_smoke.py` (15 cases: settings CRUD, runs,
graceful degradation, enable/disable, probe, registry, commands, help/tools,
intent routing, dedup, redaction).

**P3.6 — GitHub Issues/PR Tools:** Read-first GitHub project tools
for issues, PRs, and CI status. **No merge/close/delete operations
in this phase.**

|| Tool | Level | Command | What it does |
||---|---|---|---|
|| `github.status` | READ | `/github_status` | GitHub connection status |
|| `github.issues` | READ | `/github_issues [state|query]` | List issues (open/closed/all/search) |
|| `github.issue` | READ | `/github_issue <number>` | View issue details |
|| `github.prs` | READ | `/github_prs [state]` | List pull requests |
|| `github.pr` | READ | `/github_pr <number>` | View PR details |
|| `github.ci` | READ | `/github_ci [ref]` | CI status for ref/branch |
|| `github.create_issue` | WRITE_SAFE | `/github_create_issue <title> \| <body>` | Create issue (audited) |
|| `github.comment` | WRITE | `/github_comment <number> \| <body>` | Comment on issue/PR (requires confirmation) |

Environment variables:

|| Variable | Required | Default |
||---|---|---|
|| `GITHUB_TOKEN` | Yes | — (Personal Access Token) |
|| `GITHUB_DEFAULT_REPO` | Yes | — (e.g. `mammut001/Conveyor`) |
|| `GITHUB_API_BASE` | No | `https://api.github.com` |

Security: GitHub token is **never** exposed in chat replies, logs, audit
logs, or `repr()`. Creating issues is WRITE_SAFE (audited). Commenting
requires WRITE confirmation. All outputs pass through `redact_text()` +
`truncate()`.

Natural language intent: `看看 GitHub issue` → `github.issues`,
`PR 状态` → `github.prs`, `CI 挂了吗` → `github.ci`,
`创建 issue` → prompts for details.

Daily Briefing integration: If GitHub is configured, the briefing
includes open issue count, open PR count, and CI status for the
default branch.

Smoke: `scripts/github_smoke.py` (11 cases: missing config, token
redaction, command parsing, confirmation, registry, commands, help/tools,
intent routing, briefing degradation, no network).

**P3.7 — Natural Language Planner:** Planner profiles that compose
existing deterministic tools into useful personal-agent workflows.
**No new external integrations. All planner profiles are READ-only.**

| Tool | Level | Command | What it does |
|---|---|---|---|
| `planner.list` | READ | `/planners` | List all planner profiles |
| `planner.today` | READ | `/plan_today` | Today's priority analysis |
| `planner.dev` | READ | `/plan_dev` | Development plan |
| `planner.health` | READ | `/project_health` | Project health check |
| `planner.triage` | READ | `/inbox_triage` | Email triage |
| `planner.schedule` | READ | `/schedule_review` | Schedule review |

Each planner profile collects facts from READ tools (calendar,
reminders, Gmail, GitHub, notes, VPS ops) and passes them to Codex
for structured analysis. Missing integrations degrade gracefully.

Safety: **No write tools are used.** No sending emails, no creating
calendar events, no GitHub comments/issues. All collected facts pass
through `redact_text()` + `truncate()`.

Natural language intent:
- `我今天应该先干啥` → `daily_priority` planner
- `今天开发计划` → `dev_plan` planner
- `项目健康状态` / `Conveyor 有没有问题` → `project_health` planner
- `帮我整理邮件` → `inbox_triage` planner
- `今天日程安排` → `schedule_review` planner

Smoke: `scripts/planner_smoke.py` (9 cases: registry, READ-only
verification, graceful degradation, prompt building, commands,
natural language routing, planner status).

**P3.8 — Codex Job Queue:** Single-concurrency FIFO queue for Codex
jobs. New jobs are queued instead of rejected when a Codex job is
running. **Actual Codex execution remains single-concurrency.**

| Command | What it does |
|---|---|
| `/queue` | List queued/running jobs |
| `/queue_cancel <id>` | Cancel a queued job |
| `/queue_clear` | Clear all queued jobs |
| `/queue_pause` | Pause automatic dequeue |
| `/queue_resume` | Resume automatic dequeue |

Queue behavior:
- In-memory FIFO queue (lost on bot restart, documented).
- Max queue length: 10 jobs.
- When a job completes, automatically starts the next queued job.
- Queue only stores prompt text and routing metadata (no secrets).
- Redact/truncate queue display.
- Queue operations are audited when mutating.
- `/cancel` still cancels the currently running job.

Safety: **Only one Codex process at a time.** Queue is paused via
`/queue_pause`; completed jobs do not auto-start next when paused.

Smoke: `scripts/job_queue_smoke.py` (10 cases: enqueue/dequeue,
FIFO order, max length, cancel, clear, pause/resume, status display,
commands registered, help text, redaction).

**P3.9 — Generic Project Profiles:** A project skills layer that works
for any user's projects. Users define project profiles and run generic
project commands against them. Reuses existing Gmail, Calendar, GitHub,
Notes, Reminders tools.

| Command | What it does |
|---|---|
| `/projects` | List project profiles |
| `/project_add <name> \| <type> \| <desc> \| [github] \| [keywords]` | Add project (WRITE_SAFE, audited) |
| `/project_use <id>` | Set active project (WRITE_SAFE, audited) |
| `/project_show [id]` | Show project details |
| `/project_remove <id>` | Remove project (DESTRUCTIVE, requires confirmation) |
| `/project_status [id]` | Project status analysis (hybrid) |
| `/project_health [id]` | Project health check (hybrid) |
| `/project_roadmap [id]` | Project roadmap (hybrid) |
| `/project_next [id]` | Next actions for project (hybrid) |
| `/project_release_checklist [id]` | Release checklist (hybrid) |
| `/project_brief [id]` | Project brief summary (hybrid) |

Supported project types: `generic`, `mobile_app`, `web_app`, `bot`,
`library`, `research`, `course`, `business`.

Project analysis commands are READ-only. They collect facts from
configured integrations (GitHub, Notes, Gmail, Calendar, Reminders)
and use project-type-specific prompts for Codex analysis. Integrations
degrade gracefully if not configured.

Daily Briefing integration: Shows up to 3 enabled projects with short
status. Degrades gracefully if no projects configured.

Natural language routing (conservative):
- "项目列表" → `/projects`
- "切换项目 X" → `projects.use`
- "项目下一步" → `project.next`
- "项目健康状态" → `project.health`
- "项目 roadmap" → `project.roadmap`
- "发布清单" → `project.release_checklist`

Smoke: `scripts/project_profiles_smoke.py` (23 cases: CRUD, operator
isolation, active project fallback, danger levels, confirmation
requirements, briefing integration, command registration, help text,
redaction).

**P3.10 — Setup Wizard:** Makes Conveyor easier for new users to
configure after deployment. Checks existing integrations and guides
the user through setup.

| Command | What it does |
|---|---|
| `/setup` | Configuration status overview |
| `/setup_status` | Same as /setup |
| `/setup_check` | Prioritized setup checklist |
| `/setup_project` | Project setup guide |
| `/setup_gmail` | Gmail App Password guide |
| `/setup_google` | Google OAuth guide |
| `/setup_github` | GitHub Token guide |

Setup checks include:
- Telegram bot configured
- Allowed user ID configured
- Codex binary available
- Workspace root exists
- Gmail (IMAP) configured
- Google OAuth configured
- GitHub Token/Repo configured
- Daily Briefing enabled
- Active project configured

Safety: All setup commands are READ-only. Never prints token values,
app passwords, .env contents, or raw secrets. All output passes
`redact_text()` + `truncate()`.

Smoke: `scripts/setup_smoke.py` (13 cases: missing integrations,
configured status, project examples, gmail warning, github no token
leak, command registration, help text, tools list, no network calls).

**P3.11 — Project Import/Export:** Makes project profiles portable and
easier to set up. Adds import, export, and template tools for project
profiles.

| Command | What it does |
|---------|--------------|
| `/project_export [id]` | Export project(s) as JSON |
| `/project_export_all` | Export all projects |
| `/project_import <JSON>` | Import project(s) from JSON |
| `/project_template [type]` | Show project template by type |

Export JSON Schema:
```json
{
  "schema": "conveyor.project.v1",
  "projects": [
    {
      "name": "...",
      "type": "mobile_app|web_app|bot|library|research|course|business|generic",
      "description": "...",
      "github_repo": "...",
      "appstore_url": "...",
      "keywords": ["..."],
      "notes_query": "...",
      "gmail_query": "...",
      "default_branch": "...",
      "enabled": true
    }
  ]
}
```

Safety: Export does not include internal DB IDs, operator_id, tokens,
secrets, OAuth paths, or .env values. Import validates schema and
project type. Duplicate project names are skipped (not overwritten).
Import is scoped to operator_id. Imported project becomes active only
if no active project exists. Export/template are READ-only; import is
WRITE_SAFE. All output passes `redact_text()` + `truncate()`.

Smoke: `scripts/project_io_smoke.py` (15 cases: export single/all,
no ids/operator_id, valid import, skip duplicates, set active, validate
schema/type, template display, command registration, help text, no
network calls, output redacted).

**P4.1 — Web Search + Research:** Adds external web/research capability
with three-layer safety: Web Fetch → Web Search → Research.

| Command | What it does |
|---------|--------------|
| `/web_fetch <url>` | Fetch web page content |
| `/web_text <url>` | Fetch web page text |
| `/web_headers <url>` | Fetch HTTP headers |
| `/web_search <query>` | Web search (multi-backend) |
| `/research <question>` | Web research with Codex synthesis |
| `/project_research [id] <question>` | Project-context research |

**Natural language examples:**
- `search web for Python asyncio` → `/web_search Python asyncio`
- `research about AI coding assistants` → `/research AI coding assistants`
- `fetch https://example.com` → `/web_fetch https://example.com`

Supported search backends (`WEB_SEARCH_BACKEND`):
- `disabled` (default), `brave`, `tavily`, `serper`, `searxng`

Safety: All tools are READ-only. URL validation rejects localhost,
private IPs, and metadata endpoints. No file writes, no arbitrary
curl, no JS execution. All output passes `redact_text()` + `truncate()`.

**Security Hardening (P4.1.1):**
- **API key safety**: Replaced curl subprocess with urllib.request to avoid exposing API keys in process argv
- **Redirect safety**: Disabled automatic redirects (--no-location), each hop must be validated
- **Content-Type validation**: Only text/*, application/json, application/xml allowed (validated on both HEAD and GET responses)
- **IP blocking**: Expanded to include 100.64.0.0/10 (carrier-grade NAT), 198.18.0.0/15 (benchmark), multicast (224.0.0.0/4), reserved (240.0.0.0/4), IPv6 link-local (fe80::/10)
- **Metadata endpoint**: Explicit blocking for 169.254.169.254 and metadata.google.internal
- **WEB_SEARCH_ENDPOINT validation**: Rejects localhost/private/link-local/metadata endpoints
- **URL encoding**: Search queries properly encoded for all backends
- **Research uses Codex hybrid synthesis** ([HYBRID_PROMPT])
- **WEB_SEARCH_API_KEY** never appears in errors, logs, or chat output

Smoke: `scripts/web_tools_smoke.py` (31 cases), `scripts/research_smoke.py` (14 cases).

**P4.2 — File Search / Knowledge Base:** Natural-language-first file search with automatic READ-only fact collection. Slash commands are fallbacks for debugging.

| Command | What it does |
|---------|--------------|
| `/files_roots` | List search root directories |
| `/files_search <query>` | Search files |
| `/files_read <path>` | Read file content |
| `/kb_index` | Index knowledge base |
| `/kb_status` | Knowledge base status |
| `/kb_search <query>` | Search knowledge base |
| `/project_docs <query>` | Search project docs |

**Natural language examples:**
- `find deploy instructions in docs` → search files for "deploy"
- `does README have Gmail setup steps` → search files for "Gmail setup steps"
- `what does project docs say about scheduler` → search files for "scheduler"
- `summarize installation process from local docs` → search files for "installation process"
- `check my notes for OAuth content` → search files for "OAuth"

Configuration (`FILE_SEARCH_*`, `KB_*`):
- `FILE_SEARCH_ENABLED=true` — Enable file search
- `FILE_SEARCH_ALLOWED_ROOTS` — Extra allowed search roots (comma-separated)
- `FILE_SEARCH_MAX_FILE_BYTES=1000000` — Max file size
- `FILE_SEARCH_MAX_RESULTS=10` — Max results
- `FILE_SEARCH_EXTENSIONS=.md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml`
- `KB_ROOT` — Knowledge base root (default: `CODEX_MEMORY_ROOT/kb`)
- `KB_INDEX_PATH` — Index database path (default: `CODEX_MEMORY_ROOT/kb_index.sqlite`)

Safety: All file/KB analysis commands are READ-only. `kb.index` is WRITE_SAFE (audited). Rejects sensitive files (.env, secrets/, .ssh/, private keys, token files, google_token.json, client_secret.json, binary files, oversized files). All output passes `redact_text()` + `truncate()`. No file writes except KB index metadata/cache. No file deletion. No arbitrary path traversal.

Smoke: `scripts/file_search_smoke.py` (14 cases).

**P4.3 — Natural Language Agent Router:** Natural-language-first routing for all registered tools. Slash commands remain as precise fallback/debug commands.

Key features:
- Unified tool catalog built from host + personal tool registries (name, summary, danger, keywords, examples, domain)
- `/nl_help` command: lists NL examples grouped by domain
- Extended NL coverage: notes search, reminders create, calendar freebusy, queue status, setup status
- Clarification messages use natural language (no slash format suggestions)
- Safety: WRITE/DESTRUCTIVE tools never auto-execute from NL
- WRITE_SAFE tools (notes.add, reminders.create) audited when triggered by NL

**Natural language examples by domain:**

| Domain | Example | Routes to |
|--------|---------|-----------|
| Ops | `看看负载`、`磁盘空间` | load / disk |
| Notes | `记一下 xxx`、`搜索笔记里的 deploy` | notes.add / notes.search |
| Reminders | `提醒我明天9点开会` | reminders.create |
| Email | `看看最近的邮件`、`搜索邮件关于发票` | gmail.recent / gmail.search |
| Calendar | `今天有什么安排`、`下午有空吗` | calendar.today / freebusy |
| Briefing | `今日简报`、`启用简报` | briefing.today / enable |
| GitHub | `CI 挂了吗`、`看看 issue` | github.ci / issues |
| Planner | `今天应该先干啥`、`帮我整理邮件` | planner.today / triage |
| Projects | `项目列表`、`项目 roadmap` | projects.list / roadmap |
| Web | `搜索 Python asyncio`、`研究一下 React Native` | web.search / research |
| Files/KB | `找一下文档里关于 deploy` | kb.collect_facts |
| Setup | `配置状态`、`检查清单` | setup.status / check |

Safety policy:
- READ tools run automatically
- WRITE_SAFE tools (notes.add, reminders.create) run automatically but are audit-logged
- WRITE/DESTRUCTIVE tools require preview + confirmation, never auto-execute
- Ambiguous coding requests prefer Codex LLM
- Missing arguments → natural language clarification (not slash format)

Smoke: `scripts/nl_router_smoke.py` (25 cases).

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
