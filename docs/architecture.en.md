# Conveyor — Architecture & Design (EN)

> **Status**: Active
> **Date**: 2026-06-11
> **Applies to**: Channel-decoupling P0+P1, agent tool layer, Telegram live smoke

---

## 1. One-line positioning

**Conveyor is the transport layer.** Between you and `codex exec --json`,
it forwards messages per channel (Telegram / Feishu), authenticates
operators, and does lightweight preprocessing. It is **not** the agent
— the agent is Codex CLI itself (Brain + Hands); Conveyor is the
router and the carrier.

Conveyor is **orthogonal** to a "Hermes-like personal agent": Hermes
owns reasoning + tool loop; Conveyor owns "which channel the message
came from, how to deliver it to Codex, and how to ship the reply back
on the same channel."

---

## 2. Runtime architecture

```text
Telegram                            Feishu
   │                                  │
   │ Update                           │ WebSocket
   ▼                                  ▼
 bot.py                          feishu_bot.py
 _TelegramOutbound               FeishuOutbound
   │                                  │
   └──────────┬───────────────────────┘
              ▼
       InboundMessage            ← channel-agnostic
              │
              ▼
       handlers.dispatch
         · is_allowed
         · parse command
         · route_intent (deterministic | hybrid | llm)
              │
       ┌──────┼──────┬──────────────┐
       ▼      ▼      ▼              ▼
  handlers/  handlers/ handlers/  handlers/
   commands    memo     jobs       (onboarding, t-only)
       │       │       │
       └───────┴───────┘
              │
              ▼
       OutboundPort (Protocol)
              │
              ▼
        CodexRunner (unchanged)
        · worktree
        · prefetch
        · streaming
        · memo · lifecycle · metadata
```

### 2.1 VPS paths

| Use | Path |
|---|---|
| Conveyor code | `/opt/conveyor/` |
| User repo | `/srv/<your-repo>/` |
| Task root | `/srv/conveyor/` (default; configurable) |
| Daily worktree | `<task_root>/worktrees/day-YYYY-MM-DD/` |
| Feishu long connection | `wss://msg-frontier.feishu.cn/ws/v2` |

### 2.2 Layer / import boundaries

| Layer | Allowed imports | Forbidden |
|---|---|---|
| `runner/` | `config`, `redaction`, `scripts/*` | `telegram`, `lark_oapi`, `handlers` |
| `handlers/` | `runner`, `channel.types`, `redaction` | `telegram`, `lark_oapi` |
| `channel/*.py` | own SDK + `handlers` + `channel.types` | business command logic |
| `bot.py` / `feishu_bot.py` | channel adapter + `handlers.dispatch` | direct `runner` (except wire-up) |

---

## 3. Core channel types

```python
# channel/types.py — channel-agnostic, does not import telegram / lark_oapi

@dataclass(frozen=True)
class InboundMessage:
    channel: Literal["telegram", "feishu"]
    operator_id: str          # Telegram user id / Feishu open_id (str)
    chat_id: str              # session id (Telegram int → str)
    message_id: str | None    # for reply/thread
    text: str
    chat_type: Literal["p2p", "group", "unknown"]
    mentioned_bot: bool = False

class OutboundPort(Protocol):
    supports_inline_buttons: bool
    async def reply(self, msg, text) -> str | None: ...
    async def send_new(self, msg, text) -> str | None: ...
    async def edit_progress(self, msg, placeholder_id, text) -> bool: ...
    async def reply_with_buttons(self, msg, text, buttons) -> str | None: ...
```

Design notes:
- `operator_id` is always `str`; the adapter is responsible for `str(telegram_user_id)` / Feishu `open_id`.
- Handlers must not import `Update` or `FeishuChannel`.
- `OutboundPort.reply` / `send_new` return the **real `message_id`** (str) so the handler can pass it as a placeholder to `edit_progress` for in-place edits.
- `OutboundPort.edit_progress` returns `bool`:
  - **Telegram**: real in-place edit via `bot.edit_message_text`; `Message is not modified` is treated as success; other exceptions log + return `False`, the handler latch degrades to `send_new`.
  - **Feishu**: currently returns `False`; all progress goes through `send_new`. (P2.2 backlog: card streaming or throttled upgrade.)
- Duplicate-send of the closing summary was fixed in commit `09ab931`.

---

## 4. Command table

Commands live in `handlers/commands.py` `COMMAND_TABLE` and are shared
across Telegram and Feishu.

| Category | Commands |
|---|---|
| Status | `/status` `/last` `/jobs [n]` |
| Job control | `/run` `/fix` `/cancel` `/apply` `/discard` |
| Changes | `/diff` |
| Memory | `/memo` `/memory [date] [cat]` `/journal [n]` |
| Health | `/health [full] [json] [nosecurity]` `/doctor` `/diag [since]` |
| Audit | `/audit [stale-min]` `/security [since]` `/ratelimit [n]` |
| Reports | `/metrics [n]` `/log [sel]` `/meta [sel]` |
| Self-check | `/smoke` `/editcheck` |
| Maintenance | `/maintain [keep]` `/clean [keep]` |
| Help | `/help` |
| **Host ops (READ)** | `/load` `/vps` `/htop` `/ps` `/disk` `/logs` `/service_status` `/git_status` |
| **Agent tools** | `/tools` `/diagnose [server\|bot\|logs\|quick]` `/restart telegram\|feishu\|maintain` `/audit_tools [n]` |

Adding a new command = add a row to `COMMAND_TABLE` and write the
handler. Both channels pick it up.

Telegram-only routing: `bot.py` registers explicit `CommandHandler`
entries for the legacy commands and a single generic
`MessageHandler(filters.COMMAND, …)` fallback after them, so any new
slash command in `COMMAND_TABLE` is reachable without per-command
wiring. Existing explicit handlers still win first.

---

## 5. Chat-first mode and CodexRunner path

Single-operator private VPS: Codex sandbox is intentionally
`danger-full-access`, not a multi-tenant SaaS. Safety comes from the
channel allowlist, low-privilege VPS user, worktree isolation, redaction,
and explicit `/diff` + `/apply` review. Narrowing the sandbox is future
hardening — not current behavior.

| Trigger | JobMode | Codex `--sandbox` | Capabilities |
|---|---|---|---|
| Plain text | `run` | `danger-full-access` | shell, web, read/write worktree, runner CLI |
| `/run` | `run` | same | same |
| `/fix` | `fix` | same | same (kept for compatibility) |
| `记 xxx` / `/memo` | — | — | **bypasses Codex**, writes MEMORY.md directly |

Design rationale:
- Single-operator personal bot: a "must `/fix` to read an IP" boundary breaks conversational feel.
- Safety is provided by: channel allowlist, worktree isolation, `/diff` + `/apply` to merge into the main repo, and output redaction.
- `/run` vs `/fix` is kept only for legacy muscle memory and job-log separation; the **sandbox is unified as danger-full-access**.

### 5.1 Prompt injection order

Order assembled before each Codex call (see `runner/prefetch.py`):

1. `<operator-profile>` — identity, language, style
2. `<day-brief>` — cold-start summary for the first job of the day
3. `<memory-context>` — today's `MEMORY.md`
4. `<tool-registry sandbox="danger-full-access">` — shell, memorize, recall, …
5. User message

---

## 6. Boundary vs Hermes-like personal agent

| Dimension | Hermes | Conveyor |
|---|---|---|
| Agent core | Python `AIAgent` loop | **Codex CLI** |
| Tool calls | JSON Schema + dispatch | Prompt `<tool-registry>` + Codex shell |
| Multi-turn | SQLite SessionDB | one job per message (P0 backlog: session summary) |
| Channels | multi-platform | Telegram + Feishu (same business logic) |
| Memory | pluggable + Skills | MEMORY.md → JOURNAL |

Borrowed from Hermes: onboarding, day-brief, streaming chat feel, MEMORY archival.

Deliberately not duplicated: Conveyor does **not** maintain its own
tool loop, its own session DB, or its own reasoning step — all of
that is Codex CLI's job. Conveyor only ships messages between the
user and Codex, with auth + reply rendering.

---

## 6.5 Agent tool layer

Conveyor is no longer a hardcoded command bot. On top of transport it
has a **structured tool registry** and a **lightweight intent router**.

```
user message
  → route_intent()
      ├─ deterministic → handlers/tools/runner.run_tool(s)
      ├─ hybrid        → run_tools() collects facts → handle_codex_job(prompt with facts)
      └─ llm           → handle_codex_job(raw prompt)
```

### Registered tools (`handlers/tools/registry.py`)

| name | danger | description |
|---|---|---|
| `load`, `ps`, `htop`, `disk`, `logs`, `service_status`, `git_status` | READ | host snapshot, no token cost |
| `service_restart` | WRITE | restart a whitelisted conveyor unit, **requires confirmation** |

### Intent router (`handlers/intent.py` + `handlers/nl_router.py`)

- **Deterministic wins first**: explicit ops requests (load / htop / disk / logs) never go through hybrid.
- **Hybrid**: "为什么服务器慢" / "分析一下 vps" — default to `load + ps + disk + service_status`, then inject facts into the Codex prompt.
- **Explicit diagnose**: `/diagnose [server|bot|logs|quick]` (tool sets in `handlers/tools/diagnose.py`); natural-language "诊断服务器" / "帮我诊断 bot" is conservatively matched via `_DIAGNOSE_*_PATTERNS` with a `_CODING_GUARD` to avoid hijacking coding requests.
- **Ambiguous restart**: natural-language restart with no resolvable target (e.g. "重启 bot") does **not** silently default to the Telegram bot. `route_intent` returns `kind="llm"` with a clarifying `route.question`; `handlers/dispatch.py` forwards that as the Codex prompt.
- **NL router fallback** (P4.3): when intent.py's pattern matching doesn't match, falls back to `nl_router.classify_nl()` for additional domains (notes search, reminders create, calendar freebusy, queue status, setup status).
- **Tool catalog** (P4.3): `nl_router.get_catalog()` builds a unified catalog from host + personal registries, used for routing and `/nl_help`.
- **LLM fallback**: open-ended coding / debugging tasks.

### Slash commands and what they do

| Command | Behaviour |
|---|---|
| `/diagnose [mode]` | hybrid host diagnose → Codex analysis. **Not** the same as `/diag` (harness). |
| `/restart telegram\|feishu\|maintain` | whitelist alias → `service_restart` with confirmation. Arbitrary unit names are refused. |
| `/tools` | groups tools by `DangerLevel` (READ / WRITE), lists slash commands, summaries, examples, confirmation rules, and points at `/diagnose` + `/restart`. |
| `/audit_tools [n]` | reads the last `n` lines (default 10, max 50) of `audit/tools.log`; READ-only; redacted/truncated output. |

### Confirmation rules

- READ tools execute immediately.
- WRITE / DESTRUCTIVE tools call `create_pending()` and Telegram renders
  inline confirmation buttons; the text fallback is **strict** —
  accepted phrases are only `确认` / `确认执行` / `确认重启` /
  `yes confirm` / `confirm` / `execute`. Casual `好` / `ok` / `是` /
  `y` is intentionally not enough.
- Cancellation stays broad: `取消` / `算了` / `no` / `n` / `否`.
- **Context binding**: `execute_confirmed`, `cancel_pending`, and the
  text fallback all verify `operator_id + chat_id + channel` via
  `get_pending_for_context` / `matches_context`. A pending action
  cannot be confirmed from a different chat.
- `service_restart` only allows the exact whitelist:
  `conveyor-telegram-bot`, `conveyor-feishu-bot`, `conveyor-maintain.timer`.
- **Audit log**: `handlers/tools/audit.py` writes JSONL to
  `codex_memory_root/audit/tools.log` for `requested` / `confirmed` /
  `cancelled` / `executed` / `rejected` events. `arg` and result
  previews pass through `redact_text` + `truncate`. A write failure
  never breaks the user flow.

### `/ps full` safety

- `/ps` is always comm-only.
- `/ps full` returns a safety warning and points at `/ps full confirm`.
- `/ps full confirm` (if the dangerous path is enabled) prints `args`
  with a clear "full args 模式, 已 redact" header — even then,
  redaction is best-effort.

### Conservative htop routing

The `htop` regex is intentionally narrow. It only fires when there is
an execution / status context (e.g. "跑一下 htop", "运行 htop",
"check htop on server"). "look at htop source code" /
"帮我改 htop 相关代码" / "write docs about htop" route to LLM, not
the snapshot tool.

### Personal Tools Hub (P3.1 + P3.2 — local notes/reminders + delivery)

Structured foundation for future Gmail / Calendar / Contacts / GitHub
integrations. **OAuth tokens never enter Codex prompts**; Codex job
behavior is unchanged.

```
personal_tools/
  base.py      ToolResult / PersonalToolSpec / BasePersonalTool; DangerLevel reuse
  store.py     SQLite at codex_memory_root/personal_tools.db (delivery column migration)
  registry.py  notes.* / reminders.* registration + execution (passes channel/chat_id)
  notes.py     note CRUD
  reminders.py reminder CRUD + simple time parsing
  reminder_parse.py  in 10m / in 2h / tomorrow HH:MM / ISO parsing
scripts/
  scheduler_tick.py   reminder delivery scheduler (triggered by timer every 60s)
  scheduler_probe.py  scheduler probe (dry-run / live mode)
systemd/
  conveyor-scheduler.service  oneshot: runs scheduler_tick.py
  conveyor-scheduler.timer    every 60s
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| notes.add | **WRITE_SAFE** | `/note` | no confirmation; audited |
| notes.search / notes.list_recent | READ | `/notes [query]` | |
| notes.delete | DESTRUCTIVE | (API; no slash yet) | confirmation required |
| reminders.create | **WRITE_SAFE** | `/remind` | no confirmation; audited; stores channel/chat_id |
| reminders.list / reminders.due | READ | `/reminders` | |
| reminders.cancel | WRITE | (API; no slash yet) | confirmation required |

**WRITE_SAFE design decision:** `notes.add` and `reminders.create` are
low-risk append/create operations; the operator can always delete or
cancel afterwards. Requiring confirmation would break the fluency of
`/remind in 10m X`. WRITE_SAFE = no interactive confirmation, but
args + result preview are still audit-logged with redaction to
`audit/tools.log`.

**P3.2 reminder delivery:** The `reminders` table is extended via migration
with `channel`, `chat_id`, `delivered_at`, `delivery_status`, `delivery_error`,
`retry_count` columns. Migration is backward-compatible for existing DBs.
`/remind` stores `msg.channel` + `msg.chat_id` at creation time. A systemd
timer (`conveyor-scheduler.timer`) runs `scripts/scheduler_tick.py` every 60s
to find due deliverable reminders and send them as Telegram messages. Delivery
status is tracked per-reminder (`pending` → `delivered`/`failed`); failed
reminders retry up to 3 times. Supports `--dry-run` for smoke testing.

**P3.2.1 Scheduler observability:** Three deterministic tools let the operator
verify the delivery pipeline from chat without SSH:

| Tool | danger | Command | Notes |
|---|---|---|---|
| scheduler_status | READ | `/scheduler_status` | Timer/Service status + journal tail + reminder counts + channel support |
| scheduler_probe | READ | `/scheduler_probe` | Dry-run probe: runs scheduler_tick --dry-run, no network/DB writes |
| scheduler_probe_live | WRITE | `/scheduler_probe_live` | Live probe: creates test reminder and delivers to Telegram, requires confirmation |

`scheduler_status_report()` degrades gracefully when `systemctl` is unavailable (macOS/CI).
`scheduler_probe_live()` creates a `[probe]` reminder, runs `run_tick(dry_run=False)`,
then queries DB to verify `delivery_status=delivered`. All output is `redact_text()` +
`truncate()` processed; no `.env` or tokens are exposed.

Reminder time formats: `in 10m`, `in 2h`, `tomorrow HH:MM`, ISO datetime.
Parse failures return clear usage text.

`notes.delete` / `reminders.cancel` reuse the same confirmation flow and
`audit/tools.log` redaction as host tools (`handlers/tools/runner.py`).

**P3.3 Gmail App Password MVP:** Conservative Gmail integration using
IMAP + SMTP with Gmail App Password. **OAuth is a future phase.**

```
personal_tools/
  gmail_imap.py   IMAP read tools (gmail.status/recent/search/read)
  email_smtp.py   SMTP send tool (email.send, requires confirmation)
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| gmail.status | READ | `/gmail_status` | Gmail connection status |
| gmail.recent | READ | `/gmail_recent [n]` | Recent emails |
| gmail.search | READ | `/gmail_search <query>` | Search emails |
| gmail.read | READ | `/gmail_read <id>` | Read email |
| email.send | WRITE | `/email_send <to> \| <subject> \| <body>` | Send email (requires confirmation) |

Environment: `GMAIL_BACKEND=imap_smtp`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`
(16-char App Password). Optional: `GMAIL_IMAP_HOST`/`PORT`, `GMAIL_SMTP_HOST`/`PORT`.

Security: App password is **never** exposed in chat replies, logs, audit
logs, or `repr()` (`SENSITIVE_FIELDS` set). Sending requires WRITE
confirmation. No delete/archive/label in this phase. Attachments not
downloaded. `gmail.status` degrades gracefully when config is missing.

**P3.4 Google Calendar + Contacts:** Google OAuth broker with read-first
Calendar and Contacts tools. Gmail remains App Password backend; OAuth
only for Calendar/Contacts.

```
personal_tools/
  google_oauth.py      OAuth broker (status/auth/revoke, token in secrets/)
  calendar_google.py   Calendar tools (status/today/tomorrow/week/search/freebusy/create)
  contacts_google.py   Contacts tools (search)
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| google.status | READ | `/google_status` | OAuth token status |
| google.auth | WRITE | `/auth_google` | OAuth authorization flow |
| google.revoke | DESTRUCTIVE | `/google_revoke` | Revoke and delete token |
| calendar.status | READ | `/calendar_status` | Calendar connection status |
| calendar.today | READ | `/calendar_today` | Today's events |
| calendar.tomorrow | READ | `/calendar_tomorrow` | Tomorrow's events |
| calendar.week | READ | `/calendar_week` | This week's events |
| calendar.search | READ | `/calendar_search <query>` | Search events |
| calendar.freebusy | READ | `/calendar_freebusy <range>` | Check free/busy |
| calendar.create | WRITE | `/calendar_create <title> \| <time> \| <desc>` | Create event (confirm) |
| contacts.search | READ | `/contacts_search <query>` | Search contacts |

Environment: `GOOGLE_CLIENT_SECRET_PATH` (required), `GOOGLE_TOKEN_PATH`
(default `secrets/google_token.json`), `GOOGLE_OAUTH_SCOPES`,
`GOOGLE_OAUTH_REDIRECT_PORT` (default 8765).

Security: OAuth tokens stored at `codex_memory_root/secrets/google_token.json`
with chmod 600. Tokens never appear in chat, logs, audit, or `repr()`.
API errors are redacted. `calendar.create` requires WRITE confirmation.
`google.revoke` is DESTRUCTIVE. Dependencies: `google-auth`,
`google-auth-oauthlib`, `google-api-python-client`.

**TODO (later phases)**: Gmail OAuth; `github.*` tools; encrypted token
vault on VPS — Codex sees only redacted tool result summaries.

**P3.5 Daily Briefing:** Daily briefing system aggregating Calendar,
reminders, Gmail, and notes.

```
personal_tools/
  briefing.py          Briefing build and scheduling (status/today/tomorrow/enable/disable/probe)
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| briefing.status | READ | `/brief_settings` | Briefing settings status |
| briefing.today | READ | `/brief_today` | Today's briefing |
| briefing.tomorrow | READ | `/brief_tomorrow` | Tomorrow's briefing |
| briefing.enable | WRITE_SAFE | `/brief_enable [HH:MM]` | Enable daily briefing |
| briefing.disable | WRITE | `/brief_disable` | Disable daily briefing (requires confirmation) |
| briefing.probe | READ | `/brief_probe` | Briefing probe (dry-run) |

Briefing content: calendar events (requires Google OAuth), due reminders,
recent email summary (requires Gmail), recent notes. Missing providers
are gracefully degraded.

Scheduler integration: `scripts/scheduler_tick.py` checks enabled briefing
settings every minute, sends briefings when local time is reached.
`briefing_runs` table records sent dates to avoid duplicates.

Storage: `briefing_settings` (operator_id primary key, enabled/local_time/
channel/chat_id) and `briefing_runs` (operator_id + local_date unique
constraint).

Security: `briefing.enable` is `WRITE_SAFE` (only enables local setting,
audited), `briefing.disable` is `WRITE` (requires confirmation). No raw
email bodies, no OAuth tokens, no passwords. Output processed through
`redact_text()` + `truncate()`.

Natural language: `今日简报` → `briefing.today`, `启用每日简报` →
`briefing.enable`, `禁用简报` → `briefing.disable`.

Smoke: `scripts/briefing_smoke.py` (15 cases: settings CRUD, runs,
graceful degradation, enable/disable, probe, registry, commands, help/tools,
intent routing, dedup, redaction).

**P3.6 GitHub Issues/PR Tools:** Read-first GitHub project tools
for issues, PRs, and CI status. **No merge/close/delete operations
in this phase.**

```
personal_tools/
  github_tools.py      GitHub REST client (status/issues/prs/ci/create/comment)
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| github.status | READ | `/github_status` | GitHub connection status |
| github.issues | READ | `/github_issues [state\|query]` | List issues |
| github.issue | READ | `/github_issue <number>` | View issue details |
| github.prs | READ | `/github_prs [state]` | List PRs |
| github.pr | READ | `/github_pr <number>` | View PR details |
| github.ci | READ | `/github_ci [ref]` | CI status |
| github.create_issue | WRITE_SAFE | `/github_create_issue <title> \| <body>` | Create issue (audited) |
| github.comment | WRITE | `/github_comment <number> \| <body>` | Comment (requires confirmation) |

Environment: `GITHUB_TOKEN` (required), `GITHUB_DEFAULT_REPO` (required),
`GITHUB_API_BASE` (optional, default `https://api.github.com`).

Security: `github_token` is never exposed in chat replies, logs, audit
logs, or `repr()`. Create issue is `WRITE_SAFE` (audited), comment is
`WRITE` (requires confirmation). All output processed through
`redact_text()` + `truncate()`.

Natural language: `看看 GitHub issue` → `github.issues`, `PR 状态` →
`github.prs`, `CI 挂了吗` → `github.ci`, `创建 issue` → prompts for
details.

Daily Briefing integration: If GitHub is configured, briefing includes
open issue/PR counts and default branch CI status.

Smoke: `scripts/github_smoke.py` (11 cases: missing config, token
redaction, command parsing, confirmation, registry, commands, help/tools,
intent routing, briefing degradation, no network).

**P3.7 Natural Language Planner:** Planner profiles that compose
existing deterministic tools into useful personal-agent workflows.
**No new external integrations. All planner profiles are READ-only.**

```
personal_tools/
  planner.py             PlannerProfile dataclass + 5 profiles
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| planner.list | READ | `/planners` | List all planner profiles |
| planner.today | READ | `/plan_today` | Today's priority analysis |
| planner.dev | READ | `/plan_dev` | Development plan |
| planner.health | READ | `/planner_health` | Planner health check |
| planner.triage | READ | `/inbox_triage` | Email triage |
| planner.schedule | READ | `/schedule_review` | Schedule review |

Each Planner profile defines:
- `tool_items`: list of (tool_name, arg) pairs to collect (all READ)
- `prompt_template`: Codex analysis template
- `summary`: one-line description

Flow: `handle_hybrid()` → `run_tools_collected()` → Codex analysis.

Safety: Planner profiles **only use READ tools**. No email sending, no
calendar event creation, no GitHub comments/issues. All collected facts
pass through `redact_text()` + `truncate()`.

Natural language: `我今天应该先干啥` → `daily_priority`, `今天开发计划` →
`dev_plan`, `项目健康状态` → `project_health`, `帮我整理邮件` →
`inbox_triage`, `今天日程安排` → `schedule_review`.

Smoke: `scripts/planner_smoke.py` (9 cases: registry, READ-only
verification, graceful degradation, prompt building, commands,
natural language routing, planner status).

**P3.8 Codex Job Queue:** Single-concurrency FIFO queue for Codex
jobs. New jobs are queued instead of rejected when a Codex job is
running. **Actual Codex execution remains single-concurrency.**

```
handlers/
  job_queue.py             JobQueue class + QueuedJob dataclass
  jobs.py                  Queue integration in handle_codex_job
```

| Command | Notes |
|---|---|
| `/queue` | List queued/running jobs |
| `/queue_cancel <id>` | Cancel a queued job |
| `/queue_clear` | Clear all queued jobs |
| `/queue_pause` | Pause automatic dequeue |
| `/queue_resume` | Resume automatic dequeue |

Queue behavior:
- In-memory FIFO queue (lost on bot restart).
- Max queue length: 10 jobs.
- When a job completes, automatically starts the next queued job.
- Queue only stores prompt text and routing metadata (no secrets).
- Queue display is redacted/truncated via `redact_text()` + `truncate()`.
- Queue mutation operations are audited.
- `/cancel` still cancels the currently running job.

Safety: **Only one Codex process at a time.** Queue can be paused
via `/queue_pause`; completed jobs do not auto-start next when paused.

Deterministic READ tools bypass the queue and execute immediately.

Smoke: `scripts/job_queue_smoke.py` (10 cases: enqueue/dequeue,
FIFO order, max length, cancel, clear, pause/resume, status display,
commands registered, help text, redaction).

**P3.9 Generic Project Profiles:** A project skills layer that works
for any user's projects. Users define project profiles and run generic
project commands against them. Reuses existing Gmail, Calendar, GitHub,
Notes, Reminders tools.

```
personal_tools/
  store.py                 project_profiles + active_projects tables
  projects.py              Project tool implementations
  registry.py              Project tool registration
  briefing.py              Active projects integration in daily briefing
handlers/
  commands.py              Project slash commands
  intent.py                Project natural language routing
  tools/runner.py          handle_hybrid_project
```

| Command | Notes |
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
configured integrations and use project-type-specific prompts for Codex
analysis. Integrations degrade gracefully if not configured.

Safety:
- Project analysis commands are READ-only.
- `/project_add` and `/project_use` are WRITE_SAFE and audited.
- `/project_remove` is DESTRUCTIVE and requires confirmation.
- No sending emails, creating GitHub issues/comments, or creating calendar events.
- All collected facts and outputs pass `redact_text()` + `truncate()`.
- Never expose tokens, app passwords, .env values, or raw secrets.

Daily Briefing integration: Shows up to 3 enabled projects with short
status. Degrades gracefully if no projects configured.

Smoke: `scripts/project_profiles_smoke.py` (23 cases: CRUD, operator
isolation, active project fallback, danger levels, confirmation
requirements, briefing integration, command registration, help text,
redaction).

**P3.10 Setup Wizard:** Makes Conveyor easier for new users to
configure after deployment. Checks existing integrations and guides
the user through setup.

```
personal_tools/
  setup.py                 Setup tool implementations
handlers/
  commands.py              Setup slash commands
```

| Command | Notes |
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

Safety:
- All setup commands are READ-only.
- Never prints token values, app passwords, .env contents, or raw secrets.
- All output passes `redact_text()` + `truncate()`.
- No network calls.

Smoke: `scripts/setup_smoke.py` (13 cases: missing integrations,
configured status, project examples, gmail warning, github no token
leak, command registration, help text, tools list, no network calls).

**P3.11 Project Import/Export:** Makes project profiles portable and
easier to set up. Adds import, export, and template tools for project
profiles.

```
personal_tools/
  project_io.py              Import/export/template tool implementations
handlers/
  commands.py                Project import/export commands
scripts/
  project_io_smoke.py        Smoke tests
```

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/project_export [id]` | Export project(s) as JSON | READ |
| `/project_export_all` | Export all projects | READ |
| `/project_import <JSON>` | Import project(s) from JSON | WRITE_SAFE |
| `/project_template [type]` | Show project template | READ |

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

Safety:
- Export does not include internal DB IDs, operator_id, tokens, secrets,
  OAuth paths, or .env values.
- Import validates schema and project type.
- Duplicate project names are skipped (not overwritten).
- Import is scoped to operator_id.
- Imported project becomes active only if no active project exists.
- Export/template are READ-only; import is WRITE_SAFE.
- All output passes `redact_text()` + `truncate()`.

Smoke: `scripts/project_io_smoke.py` (15 cases: export single/all,
no ids/operator_id, valid import, skip duplicates, set active, validate
schema/type, template display, command registration, help text, no
network calls, output redacted).

**P4.1 Web Search + Research:** Adds external web/research capability
with three-layer safety: Web Fetch → Web Search → Research.

```
personal_tools/
  web_fetch.py               Web Fetch MVP (curl wrapper)
  web_search.py              Web Search (multi-backend)
  research.py                Research (hybrid search+fetch+Codex)
handlers/
  commands.py                Web/Research commands
scripts/
  web_tools_smoke.py         Web tools smoke tests
  research_smoke.py          Research smoke tests
```

**Phase A — Web Fetch MVP**:

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/web_fetch <url>` | Fetch web page content | READ |
| `/web_text <url>` | Fetch web page text | READ |
| `/web_headers <url>` | Fetch HTTP headers | READ |

URL validation:
- Rejects non-http/https schemes (file://, ftp://, etc.)
- Rejects localhost, 127.0.0.0/8, 0.0.0.0, ::1
- Rejects private IPs: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
- Rejects carrier-grade NAT: 100.64.0.0/10
- Rejects benchmark range: 198.18.0.0/15
- Rejects multicast: 224.0.0.0/4, ff00::/8
- Rejects reserved: 240.0.0.0/4
- Rejects link-local: 169.254.0.0/16, fe80::/10
- Rejects IPv6 ULA: fc00::/7
- Explicit blocking for metadata endpoints: 169.254.169.254, metadata.google.internal
- Resolves hostname and rejects private/reserved IP results

Curl safety:
- `--fail --silent --show-error --no-location` (redirects disabled)
- `--connect-timeout 5`
- `--max-time` (default 10s), `--max-filesize` (default 2MB)
- `--proto =http,https`
- No cookies, no auth headers, no file writes
- Content-Type validation: only text/*, application/json, application/xml (validated on both HEAD and GET responses)
- WEB_SEARCH_ENDPOINT validation: rejects localhost/private/link-local/metadata endpoints
- `shell=False` (subprocess safety)

Web Search security (P4.1.1):
- Uses urllib.request instead of curl subprocess to avoid exposing API keys in process argv
- API keys passed via HTTP headers, not in URL or command-line args
- All error messages pass through redact_text()

**Phase B — Web Search**:

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/web_search <query>` | Web search | READ |

Supported backends (`WEB_SEARCH_BACKEND`):
- `disabled` (default) — search disabled
- `brave` — Brave Search API
- `tavily` — Tavily Search API
- `serper` — Serper.dev API
- `searxng` — Self-hosted SearXNG instance

**Phase C — Research**:

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/research <question>` | Web research | READ |
| `/project_research [id] <question>` | Project research | READ |

Research flow:
1. Run web.search for search results
2. Dedupe domains
3. Fetch top N safe URLs
4. Build evidence pack (source title/url/snippet/text excerpt)
5. Return `[HYBRID_PROMPT]` marker for Codex hybrid synthesis
6. No WRITE tools used

Project research (`/project_research`):
- Uses project name, type, description, keywords, github_repo as search context
- Does not mutate project profiles
- Degrades gracefully without active project

Natural language routing:
- `search web for Python asyncio` → web.search
- `research about AI coding assistants` → research.run
- `fetch https://example.com` → web.fetch
- Prompts user in Chinese when URL/query is missing

Safety:
- All tools are READ-only
- No sending email, no creating calendar events, no GitHub writes
- No file writes, no arbitrary curl, no JS execution
- All output passes `redact_text()` + `truncate()`
- Never exposes tokens, API keys, cookies, auth headers
- No real network calls in smoke tests

Config vars:
| Variable | Default | Notes |
|----------|---------|-------|
| `WEB_FETCH_ENABLED` | true | Enable Web Fetch |
| `WEB_FETCH_TIMEOUT_SECONDS` | 10 | Timeout |
| `WEB_FETCH_MAX_BYTES` | 2000000 | Max bytes |
| `WEB_FETCH_MAX_REDIRECTS` | 3 | Max redirects |
| `WEB_USER_AGENT` | ConveyorBot/0.1 | User-Agent |
| `WEB_SEARCH_BACKEND` | disabled | Search backend |
| `WEB_SEARCH_API_KEY` | — | Search API key |
| `WEB_SEARCH_ENDPOINT` | — | Custom endpoint |
| `WEB_SEARCH_MAX_RESULTS` | 8 | Max results |
| `RESEARCH_MAX_SOURCES` | 5 | Max sources |
| `RESEARCH_FETCH_TOP_N` | 5 | Fetch top N |
| `RESEARCH_MAX_CHARS_PER_SOURCE` | 6000 | Chars per source |

Smoke:
- `scripts/web_tools_smoke.py` (31 cases: URL validation, curl safety, html_to_text, output redaction, tool danger levels, command registration, help text, disabled degradation, redirect safety, Content-Type validation, endpoint validation, URL encoding, API key safety, expanded IP blocking)
- `scripts/research_smoke.py` (14 cases: search disabled degradation, result normalization, evidence pack, READ-only tools, project research degradation, domain dedup, output redaction, hybrid prompt)

**P4.2 File Search / Knowledge Base:** Natural-language-first file search with automatic READ-only fact collection. Slash commands are fallbacks for debugging.

```
personal_tools/
  file_search.py             File search (safe boundaries)
  kb.py                      Knowledge base (SQLite FTS5)
handlers/
  commands.py                File search/KB commands
  intent.py                  Natural language routing
scripts/
  file_search_smoke.py       File search smoke tests
```

**File Search (files.search / files.read):**

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/files_roots` | List search root directories | READ |
| `/files_search <query>` | Search files | READ |
| `/files_read <path>` | Read file content | READ |

Safety boundaries:
- Only allows searching under configured roots: CODEX_WORKSPACE_ROOT, CODEX_MEMORY_ROOT/notes, KB_ROOT, FILE_SEARCH_ALLOWED_ROOTS
- Rejects sensitive files: .env, secrets/, .ssh/, private keys, token files, google_token.json, client_secret.json
- Rejects binary files (.png, .pdf, .zip, etc.)
- Rejects oversized files (exceeds FILE_SEARCH_MAX_FILE_BYTES)
- No path traversal (uses resolve() validation)
- All output passes `redact_text()` + `truncate()`

**Knowledge Base (kb.index / kb.search):**

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/kb_index` | Index knowledge base | WRITE_SAFE |
| `/kb_status` | Knowledge base status | READ |
| `/kb_search <query>` | Search knowledge base | READ |

Index storage:
- `indexed_files` table: path, root, size, mtime, sha256, ext, updated_at
- `file_chunks` table: file_id, chunk_index, text, text_hash
- Uses SQLite FTS5 (if available), otherwise LIKE fallback
- Incremental indexing: only indexes new/modified files (based on SHA256)

**Project Doc Search (/project_docs):**

| Command | Notes | Danger Level |
|---------|-------|--------------|
| `/project_docs <query>` | Search project docs | READ |
| `/project_kb_search [id] <query>` | Search project KB | READ |

- Uses project name, type, description, keywords as search context
- Does not mutate project profiles
- Degrades gracefully without active project

**Natural language routing:**
- `find deploy instructions in docs` → files.search "deploy"
- `does README have Gmail setup steps` → files.search "Gmail setup steps"
- `what does project docs say about scheduler` → files.search "scheduler"
- `summarize installation process from local docs` → files.search "installation process"
- `check my notes for OAuth content` → files.search "OAuth"
- Prompts user in Chinese when query is missing

**Auto fact collection (collect_file_facts):**
1. Search KB first (if indexed)
2. Fallback to direct file search
3. Read top N safe code snippets
4. Build evidence pack (path + excerpt)
5. Return hybrid prompt for Codex synthesis

Safety:
- All file/KB analysis commands are READ-only
- `kb.index` is WRITE_SAFE (audited)
- Never exposes secrets, tokens, API keys
- Does not include full large files in prompts
- No sending email, no creating GitHub issues, no writing calendar events
- No real network calls in smoke tests

Config vars:
| Variable | Default | Notes |
|----------|---------|-------|
| `FILE_SEARCH_ENABLED` | true | Enable file search |
| `FILE_SEARCH_ALLOWED_ROOTS` | — | Extra allowed search roots |
| `FILE_SEARCH_MAX_FILE_BYTES` | 1000000 | Max file size |
| `FILE_SEARCH_MAX_RESULTS` | 10 | Max results |
| `FILE_SEARCH_EXTENSIONS` | .md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml | Allowed extensions |
| `KB_ROOT` | CODEX_MEMORY_ROOT/kb | Knowledge base root |
| `KB_INDEX_PATH` | CODEX_MEMORY_ROOT/kb_index.sqlite | Index database path |

Smoke:
- `scripts/file_search_smoke.py` (14 cases: allowed root, path traversal rejection, .env rejection, secrets directory rejection, private key redaction, binary skip, oversized skip, files.search snippets, files.read truncation, kb.index creation, kb.search fallback, NL route trigger, project docs degradation, no network calls)

**P4.3 Natural Language Agent Router:** Natural-language-first routing for all registered tools. Slash commands remain as precise fallback/debug commands.

```
handlers/
  nl_router.py                 NL router layer + tool catalog
  intent.py                    integrates nl_router as fallback
  commands.py                  /nl_help command
scripts/
  nl_router_smoke.py           NL router smoke tests
```

**Tool Catalog:**
- Built from host TOOL_REGISTRY + personal PERSONAL_TOOL_REGISTRY
- Each entry: name, summary, danger, keywords, examples_zh, examples_en, domain
- Used for route matching and /nl_help output

**Route Classification:**

| Category | Description | Execution Policy |
|----------|-------------|------------------|
| READ_DETERMINISTIC | Direct read tool | Auto-execute |
| READ_HYBRID | Collect facts + Codex synthesis | Auto-collect, Codex synthesizes |
| WRITE_PREVIEW | Write operation | Preview + confirmation |
| CLARIFY | Missing argument | Natural language prompt |
| CODEX_LLM | Coding/open task | Codex handles |

**Extended NL Coverage (P4.3):**
- Notes search: `搜索笔记里的 deploy` → notes.search
- Reminders create: `提醒我明天9点开会` → reminders.create
- Calendar freebusy: `下午有空吗` → calendar.freebusy
- Queue status: `队列状态` → scheduler_status
- Setup status: `配置状态` → setup.status

**Safety Policy:**
- READ tools run automatically
- WRITE_SAFE tools (notes.add, reminders.create) run automatically but are audit-logged
- WRITE/DESTRUCTIVE tools require preview + confirmation, never auto-execute
- Ambiguous coding requests prefer Codex LLM
- Missing arguments → natural language clarification (not slash format)
- Confirmation messages don't include slash format suggestions

**/nl_help output:**
Groups examples by domain: Ops, Notes, Reminders, Email, Calendar, Contacts, Briefing, GitHub, Planner, Projects, Setup, Web, Research, Files, KB, Scheduler.

Smoke:
- `scripts/nl_router_smoke.py` (25 cases: catalog build, catalog fields, NL routing for calendar/email/GitHub/KB/research/notes/reminders/queue/setup, ambiguous coding → LLM, no slash in clarification, /nl_help output, /nl_help domains, /nl_help registered, WRITE_SAFE marking, READ marking, project patterns, NL examples, slash commands importable, coding guard, notes add, web search, gmail search)

---

## 6.6 Telegram live smoke

The harness suite covers the agent tool layer; for real Telegram
end-to-end testing, Conveyor ships a manual live smoke that drives
the running bot **as a real Telegram user** via Telethon.

### Why Bot API is not enough

Messages sent through the Telegram Bot API do not trigger the bot's
own `MessageHandler` (you can't make the bot talk to itself). To
exercise the live `MessageHandler` path, the test must impersonate a
user with a real Telegram user client.

### Telethon user client

`scripts/telegram_live_smoke.py` connects via Telethon, resolves the
bot entity by username, sends each test message, then polls the
recent chat history for new bot messages until the expected needles
match or the timeout expires. Edited messages are picked up too
(Conveyor may edit a placeholder in place).

### Required env vars

| Var | Required | Default |
|---|---|---|
| `TELEGRAM_API_ID` | yes | — |
| `TELEGRAM_API_HASH` | yes | — |
| `TELEGRAM_BOT_USERNAME` | yes (or `--bot`) | — |
| `TELEGRAM_TEST_SESSION` | no | `.telegram-live-smoke` |
| `TELEGRAM_LIVE_TIMEOUT` | no | 45s |
| `TELEGRAM_LIVE_ALLOW_RESTART` | no (gate #1) | unset |
| `TELEGRAM_LIVE_RESTART_TARGET` | no | `telegram` |

### Commands

```bash
pip install telethon
export TELEGRAM_API_ID=...
export TELEGRAM_API_HASH=...
export TELEGRAM_BOT_USERNAME=your_bot_username
.venv/bin/python scripts/telegram_live_smoke.py --quick
.venv/bin/python scripts/telegram_live_smoke.py --full
```

`--quick` runs six safe assertions
(`/tools`, `/load`, `/ps`, `/ps full`, `重启 bot` must not default to
telegram, `/audit_tools`).
`--full` adds the Codex-path and restart-cancellation checks
(`/diagnose quick`, `跑一下 htop`, `/restart telegram` → cancel,
`重启 feishu bot` → cancel, `look at htop source code` to LLM).

### Restart safety gates

Restart confirmation is **cancelled by default**: every
restart-creating command is followed by `取消`. To actually restart a
conveyor unit, **both** gates must be open:

1. env `TELEGRAM_LIVE_ALLOW_RESTART=1`
2. CLI `--allow-restart`

Even then, the target is validated against the whitelist
`telegram|feishu|maintain`, a `DANGER` warning is printed, and
`run_simple` requires at least one bot reply to PASS.

The script never prints bot tokens, api hash, session paths, or
`.env` content. `.telegram-live-smoke*` and `*.session` are
git-ignored.

Exit codes:
- `0` all selected tests passed
- `1` one or more tests failed
- `2` missing optional dependency (telethon) or required env config
- `3` Telethon connection / auth error

## 6.7 Progress verbosity policy

Codex streaming events are noisy on the chat surface: placeholder,
"我这就帮你查一下。" style agent prose, "🔧 curl..." tool
indicators, the round-5 thinking indicator, and the round-6 tool
pulse can each become a fresh bubble. Feishu cannot edit_progress,
so every progress becomes a new message and the user sees
"⏳ Got it / Sure, looking into it / 🔧 curl / By the way... / 🔧
curl / final".

A new env var `CONVEYOR_PROGRESS_MODE` (default `compact`) controls
the verbosity:

| mode | prose progress | tool indicator | thinking indicator | tool pulse | fallback after edit failure |
| --- | --- | --- | --- | --- | --- |
| `verbose` (debug) | sent | sent | sent | sent | legacy: every progress is a new message |
| `compact` (default) | **dropped** | sent | sent | sent | **at most one** "仍在处理..." line |
| `quiet` | dropped | dropped | dropped | dropped | nothing |

`handlers/jobs.py::progress()` also enforces the policy a second
time, and the final `job.summary` is still sent exactly once (with
the existing strip-based de-dup vs `last_progress`). Feishu
benefits the most: under `quiet` the user sees only the placeholder
and the final answer, with no "curl/curl/curl" chain.

**Configuration**:

- `CONVEYOR_PROGRESS_MODE=verbose|compact|quiet`
- Default: `compact`
- Unknown values fall back to `compact` with a warning so a bad
  `.env` cannot brick a deploy.

**Tests**:

- `scripts/jobs_progress_mode_smoke.py` — 6 behavior groups + config
  parsing (19/19 case).
- The older `scripts/progress_smoke.py` and
  `scripts/jobs_dedupe_smoke.py` force `verbose` to pin the legacy
  contract; their behavior is unchanged.

`scripts/telegram_live_helpers_smoke.py` covers the pure helpers
(`redact`, `validate_restart_target`) and **is** part of
`make smoke`; the live script itself is manual only.

---

## 7. Harness matrix

```text
make smoke
  ├── runner smokes (unchanged)
  │     auto_maintain / compress_day / clean_* / classify_memo /
  │     memo_flow / memo_fastpath / progress
  ├── handlers smokes (channel-agnostic)
  │     handlers_smoke / jobs_dedupe_smoke
  │     ops_intent_smoke / ops_smoke / ops_run_smoke / telegram_outbound_smoke
  │     tools_intent_smoke / tools_runner_smoke
  │     telegram_command_fallback_smoke / confirm_strict_smoke / ps_full_smoke
  │     diagnose_command_smoke / restart_alias_smoke / tools_output_smoke
  │     confirmation_context_smoke / tool_audit_smoke / audit_tools_smoke
  │     telegram_live_helpers_smoke
  │     docs_consistency_smoke
  │     channel_telegram_smoke / channel_feishu_smoke
  │     import_boundary_smoke
  │     jobs_progress_mode_smoke        ← CONVEYOR_PROGRESS_MODE, 6 groups
  │     deploy_workflow_smoke           ← deploy script static checks
  │     deploy_status_smoke             ← /deploy_status command
  └── command_harness
        38 cases, drives handlers.dispatch + FakeOutbound + FakeRunner
        (no more FakeUpdate / FakeMessage / FakeContext)
```

`scripts/telegram_live_smoke.py` is **not** in `make smoke`; it is a
manual live script that needs real Telegram credentials and a
Telethon install.

After P2.1: `channel/telegram.py` and `channel/feishu.py` own the
adapter logic; the entrypoint files just wire handlers, onboarding,
and the long-lived `_start_job` / `tool_callback` paths. Channel
behavior is now testable in isolation; the layer rule is enforced
statically by `import_boundary_smoke.py`.

---

## 8. Phase progress

| Phase | Status | commit |
|---|---|---|
| P0 handler extraction, zero behavior change | done | `8828489` |
| P1 command-table unification + harness migration | done | `8828489` |
| P1.x dedupe closing summary | done | `09ab931` |
| Agent tool layer (registry / router / runner / confirm / audit) | done | `eddf1ba` |
| Host ops fast path (`/load` `/vps` `/htop` `/ps` `/disk` `/logs` `/service_status` `/git_status`) | done | — |
| `/diagnose` + `/restart` aliases + `/audit_tools` | done | — |
| Telegram live smoke (real user, Telethon) | done | `eddf1ba` |
| docs bilingual sync | done | (this task) |
| P2.1 Adapter split (`channel/telegram.py`, `channel/feishu.py`) | done | (this task) |
| `CONVEYOR_PROGRESS_MODE` (verbose/compact/quiet) | done | (this task) |
| P2.2 Feishu progress card / throttle | done | (this task) |
| P2.3 Onboarding extraction | done | (this task) |
| P2.4 Single-process dual-channel | backlog | — |
| P2.4 Session summary | done | (this task) |
| P2.5 Audit log rotation | done | (this task) |
| Auto VPS deploy (GitHub Actions) | done | `fa93606` |

---

## 9. Next backlog candidates

Ordered by impact-to-effort ratio. **Recommended next implementation
order**: P2.1 → P2.2 → P2.4. The other two (P2.3, P2.5) can be
picked up opportunistically.

### P2.1 Adapter split (done)

- Telegram adapter lives in `channel/telegram.py`:
  `TelegramOutbound` / `inbound_from_update` / `make_outbound` /
  `send_text` / `edit_text`.
- Feishu adapter lives in `channel/feishu.py`:
  `FeishuOutbound` / `inbound_from_event`.
- Channel-level smokes: `channel_telegram_smoke.py` /
  `channel_feishu_smoke.py`. The boundary rule is enforced
  statically by `import_boundary_smoke.py` (AST scan: no Telegram
  SDK in `handlers/`, no lark_oapi in `channel/telegram.py`, no
  Telegram SDK in `channel/feishu.py`, no `runner` import in
  `channel/*.py`).
- `bot.py` / `feishu_bot.py` are now entrypoints only: handler
  registration, onboarding, `tool_callback` wiring, and the Feishu
  WebSocket connect. The legacy `_start_job` / `_typing_loop` (which
  bypassed `TelegramOutbound`) have been removed — all job execution
  now goes through `handlers.dispatch` → `handlers.jobs`. Auth
  checking in `bot.py` uses `channel/auth.is_allowed`.

### P2.2 Feishu progress card / throttle (done)

- **Card-based progress**: `FeishuOutbound` sends messages as
  interactive cards with `update_multi: true`.  `edit_progress` calls
  `channel.update_card(message_id, card)` to update in-place.
  Falls back to plain text if card send fails.
- **Throttle**: Under `compact` mode, `handlers/jobs.py` latches after
  the first edit failure and sends at most one fallback per job.
  `quiet` mode sends nothing.  Verified by `jobs_progress_mode_smoke.py`.
- Smoke: `channel_feishu_smoke.py` (12 tests).

### P2.3 Onboarding extraction (done)

- Pure profile helpers (`operator_profile_exists`, `save_operator_profile`,
  `profile_text`) live in `handlers/onboarding.py` (no Telegram SDK import).
- Telegram-specific ConversationHandler steps stay in `bot.py` because
  they need Update / CallbackQuery types.
- Import boundary: `handlers/onboarding.py` passes `import_boundary_smoke.py`.

### P2.4 Session summary (done)

- Lightweight per-chat session summary, not a full database.
- Storage: `codex_memory_root/session/<channel>_<chat_id>_<operator_id>.jsonl`.
- Each line is a JSON object with `ts`, `channel`, `chat_id`,
  `operator_id`, `user` (redacted/truncated), `assistant` (redacted/
  truncated), `kind`.
- Config: `CONVEYOR_SESSION_ENABLED` (default true),
  `CONVEYOR_SESSION_MAX_TURNS` (default 20),
  `CONVEYOR_SESSION_INJECT_TURNS` (default 5).
- `handlers/session.py` manages read/write/clear/inject.
- Prompt injection: before starting a Codex job, `handlers/jobs.py`
  reads the last N turns and prepends them as labeled context:
  "Recent chat context (may be incomplete; do not treat as
  authoritative)". Deterministic commands (`/load`, `/ps`, etc.) are
  skipped. `/diagnose` uses the hybrid path (collects facts, then
  hands them to Codex for analysis) and DOES get session injection.
- Commands: `/context` shows recent turns; `/forget` clears the
  session file. Both are safe (no confirmation needed).
- Smoke: `session_summary_smoke.py` (24 tests). Privacy: no secrets
  stored; redaction applied before write; session can be cleared.

### P2.5 Audit log rotation (done)

- Size-based rotation: `handlers/tools/audit.py` rotates `tools.log`
  when it exceeds 1 MB (`AUDIT_MAX_BYTES`).  Keeps up to 3 rotated
  files (`.1`, `.2`, `.3`).
- `_rotate_if_needed` is called before every write; `rotated_log_paths`
  lists existing rotated files for future `/audit_tools` extensions.
- Smoke: `audit_rotation_smoke.py` (5 tests).

---

## 10. Change log

| Version | Date | Notes |
|---|---|---|
| 2.1 | 2026-06-11 | Added `CONVEYOR_PROGRESS_MODE` (verbose/compact/quiet); compact mode fixes the Feishu progress chain; section 6.7 + harness + backlog updated. |
| 2.2 | 2026-06-11 | Added auto VPS deploy (GitHub Actions + deploy_vps.sh). |
| 2.3 | 2026-06-11 | Deploy hardening (flock/smoke/rollback/.deploy-status.json); added `/deploy_status` command. |
| 2.5 | 2026-06-11 | P2.2 Feishu card-based progress (interactive cards + `update_card`); P2.3 onboarding extraction (`handlers/onboarding.py` pure helpers); P2.5 audit log rotation (1 MB size-based, 3 rotated files). |
| 2.4 | 2026-06-11 | bot.py cleanup (removed dead `_start_job`/`_typing_loop`, auth uses `is_allowed`); P2.4 session summary (`handlers/session.py`, `/context`, `/forget`, prompt injection). |
| 2.0 | 2026-06-11 | English translation, added agent tool layer, Telegram live smoke, bilingual sync. |
| 1.0 | 2026-06-09 | Original Chinese architecture doc. |
