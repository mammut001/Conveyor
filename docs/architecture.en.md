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

| Trigger | JobMode | Codex `--sandbox` | Capabilities |
|---|---|---|---|
| Plain text | `run` | `workspace-write` | shell, web, read/write worktree, runner CLI |
| `/run` | `run` | same | same |
| `/fix` | `fix` | same | same (kept for compatibility) |
| `记 xxx` / `/memo` | — | — | **bypasses Codex**, writes MEMORY.md directly |

Design rationale:
- Single-operator personal bot: a "must `/fix` to read an IP" boundary breaks conversational feel.
- Safety is provided by: channel allowlist, worktree isolation, `/diff` + `/apply` to merge into the main repo, and output redaction.
- `/run` vs `/fix` is kept only for legacy muscle memory and job-log separation; the **sandbox is unified**.

### 5.1 Prompt injection order

Order assembled before each Codex call (see `runner/prefetch.py`):

1. `<operator-profile>` — identity, language, style
2. `<day-brief>` — cold-start summary for the first job of the day
3. `<memory-context>` — today's `MEMORY.md`
4. `<tool-registry sandbox="workspace-write">` — shell, memorize, recall, …
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
| P2.2 Feishu progress card / throttle | backlog | — |
| P2.3 Onboarding extraction | backlog | — |
| P2.4 Single-process dual-channel | backlog | — |
| Session summary / multi-turn continuity | backlog | — |
| Audit log rotation | backlog | — |

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
  registration, onboarding, the long-lived `_start_job` /
  `tool_callback` wiring, and the Feishu WebSocket connect. The
  adapter volume (~150 lines) is gone from each file.

### P2.2 Feishu progress card / throttle (second)

- Current state: `channel/feishu.py::FeishuOutbound.edit_progress`
  always returns `False`, so under `compact`/`quiet` the placeholder
  edit failure produces at most one fallback "仍在处理..." line.
- If Feishu eventually supports patching message cards, point
  `edit_progress` at the card-update API and re-enable in-place
  edits.
- If only full re-sends are supported, the mode-aware cap already
  in place (≤ 1 line in compact, 0 in quiet) is enough.
- Rationale: `CONVEYOR_PROGRESS_MODE=quiet` already eliminates the
  spam; the priority is lower than it was pre-P2.7.

### P2.3 Onboarding extraction

- Move the onboarding state machine out of `bot.py` into `handlers/onboarding.py` (or a channel-aware handler).
- Rationale: `bot.py` is too large.

### P2.4 Session summary (third)

- Lightweight per-chat session summary, not a full database.
- Store last N turns / tool facts in `codex_memory_root/session`.
- Inject compact context before Codex for "continue / 刚才那个" style requests.
- Rationale: currently every message starts a fresh Codex job.

### P2.5 Audit log rotation

- Rotate `audit/tools.log` by size or date.
- Add `/audit_tools clear` only if gated by confirmation.
- Rationale: the audit JSONL grows unbounded; no retention policy yet.

---

## 10. Change log

| Version | Date | Notes |
|---|---|---|
| 2.1 | 2026-06-11 | Added `CONVEYOR_PROGRESS_MODE` (verbose/compact/quiet); compact mode fixes the Feishu progress chain; section 6.7 + harness + backlog updated. |
| 2.0 | 2026-06-11 | English translation, added agent tool layer, Telegram live smoke, bilingual sync. |
| 1.0 | 2026-06-09 | Original Chinese architecture doc. |
