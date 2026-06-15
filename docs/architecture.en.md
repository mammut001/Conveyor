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

### Intent router (`handlers/intent.py`)

- **Deterministic wins first**: explicit ops requests (load / htop / disk / logs) never go through hybrid.
- **Hybrid**: "为什么服务器慢" / "分析一下 vps" — default to `load + ps + disk + service_status`, then inject facts into the Codex prompt.
- **Explicit diagnose**: `/diagnose [server|bot|logs|quick]` (tool sets in `handlers/tools/diagnose.py`); natural-language "诊断服务器" / "帮我诊断 bot" is conservatively matched via `_DIAGNOSE_*_PATTERNS` with a `_CODING_GUARD` to avoid hijacking coding requests.
- **Ambiguous restart**: natural-language restart with no resolvable target (e.g. "重启 bot") does **not** silently default to the Telegram bot. `route_intent` returns `kind="llm"` with a clarifying `route.question`; `handlers/dispatch.py` forwards that as the Codex prompt.
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

### Personal Tools Hub (P3.1 — local notes/reminders)

Structured foundation for future Gmail / Calendar / Contacts / GitHub
integrations. **OAuth tokens never enter Codex prompts**; Codex job
behavior is unchanged.

```
personal_tools/
  base.py      ToolResult / PersonalToolSpec / BasePersonalTool; DangerLevel reuse
  store.py     SQLite at codex_memory_root/personal_tools.db
  registry.py  notes.* / reminders.* registration + execution
  notes.py     note CRUD
  reminders.py reminder CRUD + simple time parsing
  reminder_parse.py  in 10m / in 2h / tomorrow HH:MM / ISO parsing
```

| Tool | danger | Command | Notes |
|---|---|---|---|
| notes.add | **WRITE_SAFE** | `/note` | no confirmation; audited |
| notes.search / notes.list_recent | READ | `/notes [query]` | |
| notes.delete | DESTRUCTIVE | (API; no slash yet) | confirmation required |
| reminders.create | **WRITE_SAFE** | `/remind` | no confirmation; audited |
| reminders.list / reminders.due | READ | `/reminders` | |
| reminders.cancel | WRITE | (API; no slash yet) | confirmation required |

**WRITE_SAFE design decision:** `notes.add` and `reminders.create` are
low-risk append/create operations; the operator can always delete or
cancel afterwards. Requiring confirmation would break the fluency of
`/remind in 10m X`. WRITE_SAFE = no interactive confirmation, but
args + result preview are still audit-logged with redaction to
`audit/tools.log`.

Reminder time formats: `in 10m`, `in 2h`, `tomorrow HH:MM`, ISO datetime.
Parse failures return clear usage text.

`notes.delete` / `reminders.cancel` reuse the same confirmation flow and
`audit/tools.log` redaction as host tools (`handlers/tools/runner.py`).

**TODO (later phases)**: Google OAuth broker; `gmail.*` / `calendar.*` /
`contacts.*` / `github.*` tools; encrypted token vault on VPS — Codex
sees only redacted tool result summaries; reminder scheduler delivery
(cron/timer).

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
