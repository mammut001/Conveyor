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
  `workspace-write`
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
- `/help` — full command list

### Agent tool layer

Conveyor is **not** only a hardcoded command bot. A structured tool
registry plus a lightweight intent router sit between chat input and
Codex:

| Path | When | Example |
|---|---|---|
| **Deterministic** | Stable host checks | `看看磁盘`, `/logs`, `git status` |
| **Hybrid** | Diagnosis / “why” questions | `为什么服务器这么慢` → collect load/ps/disk/service facts, then Codex analyzes |
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
(Telegram inline buttons; Feishu/text replies `确认` / `取消`).

Implementation: `handlers/tools/` (registry + executors + runner),
`handlers/intent.py` (`route_intent`). Handlers stay channel-agnostic;
Telegram callbacks use `tool:confirm:<token>` / `tool:cancel:<token>`.

### Deterministic host ops (legacy slash commands)

These slash commands and matching natural-language phrases still work
and map into the tool layer above:

| Command | Phrasing | What it does |
|---|---|---|
| `/load` (alias `/vps`) | `看看我的负载`, `check vps load` | Hostname, time, uptime, CPU count, memory, disk for `/ /srv /opt`, top CPU/mem processes. |
| `/htop` | `跑一下 htop`, `top 看一下` | htop is a TUI; returns a `top -bn1` snapshot with a one-line TUI explanation. |
| `/ps` (or `/ps full`) | `ps aux`, `哪些进程` | Top processes by CPU/mem. Default uses `comm` (no argv → no token leak). `full` includes args (still passed through redaction). |

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

- Channel access is denied unless the sender id exactly matches
  `TELEGRAM_ALLOWED_USER_ID` or `LARK_ALLOWED_OPEN_ID`. There is no
  other authentication; the ALLOWED_* gate is the only thing
  standing between this bot and the public internet.
- Prompts are passed only to Codex stdin; they are never executed
  as shell commands.
- `/run` and plain text use Codex `workspace-write` in the daily
  worktree (chat-first; see `docs/architecture.md` §5). `/fix` is
  an alias with the same sandbox.
- `danger-full-access` is never used.
- Each job uses a detached git worktree created from `HEAD`.
- Raw Codex JSONL stays on disk; Telegram / Feishu output is
  truncated and redacted for common secret patterns.
- The systemd units set `PYTHONDONTWRITEBYTECODE=1` so runtime
  imports do not leave `__pycache__` files in the deployed
  directory.
- The bot does **not** commit, push, or merge changes. Apply is
  always an explicit `/apply` after you review `/diff`.

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
| Telegram replies are very slow | `TEGRAM_PROGRESS_SECONDS` (default 3s) controls placeholder edits; rate limit is 20 edits/min on Telegram |

---

## 9. License

MIT — see [`LICENSE`](LICENSE).
