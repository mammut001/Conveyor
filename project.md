# conveyor — project dossier

Comprehensive reference for the project. **README.md** is the install-and-run
quickstart; **CHANGELOG.md** is the change history and current surface at a
glance; this file is the design + deploy + invariants + open-items brief a
new session needs to be useful fast.

Snapshot at HEAD `b4540d5` (2026-06-05, America/Toronto). 47 commits on
`main`. No git remote configured. Working tree is clean. 93/93 smokes
green (progress_smoke 19 -> 23 cases after chat-feel round 2; 23 -> 26
cases after chat-feel round 4; 26 -> 30 cases after chat-feel round
5; 30 -> 32 cases after chat-feel round 7; 32 -> 36 cases after
chat-feel round 6; 36 -> 39 cases after chat-feel round 8; 39 -> 42
cases after onboarding A+B round; 42 -> 44 cases after onboarding C
round). Round 2's
`"shell"`; round 3 extracts the actual binary name and surfaces
`🔧 curl...` / `🔧 python...` etc. (falling back to `🔧 shell...` for
empty / unparseable commands); round 4 adds a per-item growing gate
on prose events so a mid-stream paragraph rewrite no longer re-edits
the placeholder to a shorter string; round 5 surfaces a "💭
thinking..." indicator after >1.0s of sustained reasoning so a hard
think (math, multi-step planning, debugging) does not look frozen;
round 6 surfaces a periodic `🔧 name (Ns)...` tool-pulse every 4s
while a tool call is in flight so a long-running tool call does not
look frozen; round 7 short-circuits identical `progress()` payloads
before the wire so `BadRequest: Message is not modified` cannot
trip the bot's edit-broken latch; round 8 seeds `_read_jsonl_stdout`'s
`last_sent` at `-telegram_progress_seconds` so the first prose edit
lands immediately after the placeholder (T+0) instead of waiting 3s
for the shared cooldown to elapse, after which the normal 3s cooldown
applies for the rest of the stream (one-shot bypass, not a permanent
lowering of the cooldown). Onboarding A prepends an
`<operator-profile>` block to every prompt with the 4 attrs the
agent always needs (name / language / style / standing) sourced
from .env (OPERATOR_NAME / LANGUAGE / STYLE / STANDING; defaults
to anonymous / zh-CN / terse / personal-scale, single operator),
so the agent no longer re-discovers who the operator is on every
session; onboarding B delivers a one-shot `<day-brief>` on the
first job of each user-local day, with 3 sections (yesterday's
journal preview, today's MEMORY.md preview, last 3 jobs'
summaries), state at codex_memory_root/state/last_day_brief.txt,
so the first message of the day does not feel like a cold start.
Onboarding C adds the first-run `/onboard` 3-step Q&A (name /
language / style) that writes a persistent
`codex_memory_root/operator.json` profile, loaded at startup with
`operator.json > .env > default` resolution; the first run detects
no operator.json and `start_cmd` + `text_cmd` both nudge
`/onboard` instead of silently starting a job, mirroring the
Hermes-style "first time" experience. Hot-reload
(`b4540d5`) makes the operator.json re-read on every
prefetch so /profile edits or manual ssh edits take effect
on the next job without a bot restart. Onboarding C button (`0c13cdc`) adds a one-tap inline
"开始 onboarding" button to the first-run /start and first-message
nudges, so the user does not have to type /onboard after reading
the welcome — the button drives the same ConversationHandler
entry point (callback_data="ob:start"). Telegram bots cannot
send proactive messages to users who have not messaged them
first, so this is the best UX in that constraint.

---

## 1. What this is

A small Python service that lets **one whitelisted Telegram user** drive
`codex exec --json` jobs on an Ubuntu VPS, plus a 5-category MEMORY.md
capture loop that survives across days. Personal-scale; one operator, one
machine, fixed tool set.

Two systemd units do the actual work:

- `conveyor-telegram-bot.service` — long-running Telegram listener + codex
  subprocess spawner
- `conveyor-feishu-bot.service` — Feishu listener (same command surface)
- `conveyor-maintain.service` + `conveyor-maintain.timer` —
  hourly self-maintenance (GC, snapshot, compress, unfiled reclassify)

Maintain is a separate unit on purpose: a maintain failure does not take the
bot down.

---

## 2. Repository layout

Lives at the repo root (this directory). As of 2026-06-05 the
repo is no longer nested inside any unrelated project directory;
previously it lived as a nested git repo inside a different
worktree, but that nesting is no longer in use.

```text
conveyor/
  bot.py                          24 KB   Telegram command handlers
  config.py                        4 KB   .env loading + Settings dataclass
  runner.py                       60 KB   CodexRunner, job queue, worktrees, codex subprocess
  redaction.py                     2 KB   Output redaction + truncation
  requirements.txt                 48 B
  .env.example                   600 B    template (no secrets)
  .env                            -      local only, .gitignored, contains live secrets
  Makefile                        21 L    `make smoke` pre-deploy gate
  README.md                       13 KB   install + commands quickstart
  CHANGELOG.md                    6 KB    surface + 17-commit timeline + Honest gaps
  project.md                      (this)  design + deploy + invariants
  systemd/
    conveyor-telegram-bot.service
    conveyor-feishu-bot.service
    conveyor-maintain.service
    conveyor-maintain.timer
  scripts/
    deploy.sh                              rsync + restart
    auto_maintain.py                       hourly maintenance harness
    auto_maintain_smoke.py
    compress_day.py                        yesterday's MEMORY.md -> ~/.codex/JOURNAL/YYYY-MM-DD.md
    compress_day_smoke.py
    clean_worktrees_smoke.py               GC selection logic for old daily worktrees
    clean_old_jobs_smoke.py                GC selection logic for old per-job log dirs
    classify_memo_smoke.py                 LLM-based memo classifier contract
    memo_flow_smoke.py                     append_memo + reclassify_unfiled contract
    memo_fastpath_smoke.py                 _handle_memo_fast_path routing contract
    memo_smoke.py                          full integration, needs populated .env
    submit_job.py                          operator CLI: --mode run|fix "prompt"
```

---

## 3. VPS deploy

### 3.1 Endpoints

| What | Value |
|---|---|
| Host | `<ssh-user>@<vps-host>` (set via `CONVEYOR_REMOTE`, see §3.3) |
| Runtime path | `/opt/conveyor/` |
| Codex CLI home (uv tool dir) | `~/.codex/` (under the `ubuntu` user) |
| Bot systemd unit | `/etc/systemd/system/conveyor-telegram-bot.service` |
| Feishu bot unit | `/etc/systemd/system/conveyor-feishu-bot.service` |
| Maintain unit + timer | `/etc/systemd/system/conveyor-maintain.{service,timer}` |
| Workspace | `/srv/codex-telegram-test-repo/` (the user's project; git operations and codex runs) |
| Worktree root | `/srv/conveyor/worktrees/` (per-day `day-YYYY-MM-DD/` and `/fix` worktrees; all `git worktree add` of workspace) |
| Health snapshots | `/srv/conveyor/health/latest-{fast,full}.json` |

The VPS is **not** a git repo. `/opt` has no `.git`. Push semantics in this
project are local-commit + rsync; VPS commits are not a thing.

### 3.2 deploy.sh

`scripts/deploy.sh` is the canonical deploy. It is driven by two env
variables with sensible defaults:

```bash
CONVEYOR_REMOTE=<ssh-user>@<vps-host>
CONVEYOR_REMOTE_DIR=/opt/conveyor
```

Steps (verbatim from the script):

1. For each of `scripts bot.py config.py runner.py redaction.py
   requirements.txt systemd`, run
   `rsync -avz --exclude=.env --exclude=.env.* --exclude=__pycache__
   --exclude=*.pyc --exclude=.venv LOCAL/... REMOTE:/opt/...`
2. SSH to the VPS and:
   - `rm -rf /opt/conveyor/scripts/__pycache__
     /opt/conveyor/__pycache__`
   - `sudo systemctl restart conveyor-telegram-bot.service`
   - `sleep 2`
   - `sudo systemctl is-active conveyor-telegram-bot.service` (asserts active)

The script's preamble warns: **never** `rsync --delete` against the project
root on the VPS — `.env` lives there and contains live bot/LLM secrets; the
local copy intentionally omits it. Use a per-subdir rsync list with
`--exclude=.env*`.

### 3.3 What is NOT in the rsync list

These are deployed manually when they change:

- `CHANGELOG.md` — not in the script; sync with
  `rsync -avz CHANGELOG.md $REMOTE:/opt/conveyor/CHANGELOG.md  # $REMOTE = $CONVEYOR_REMOTE`
- `README.md` — same manual treatment if you care about the live doc
- `Makefile` — same; the bot service runs `bot.py` directly, Makefile is
  local-only
- `.env` — local-only, never deploy. The VPS `.env` is provisioned at
  install time and lives in `/opt/conveyor/.env`
- `.venv/` — provisioned on the VPS by `python3 -m venv .venv &&
  .venv/bin/pip install -r requirements.txt`

### 3.4 systemd units

**conveyor-telegram-bot.service** (always-on, restart on crash):

```ini
[Service]
Type=simple
User=ubuntu / Group=ubuntu
WorkingDirectory=/opt/conveyor
EnvironmentFile=/opt/conveyor/.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/conveyor/.venv/bin/python /opt/conveyor/bot.py
Restart=on-failure / RestartSec=5
NoNewPrivileges=true / PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/conveyor /srv /home/ubuntu
```

**conveyor-maintain.service** (oneshot, kicked by the timer):

```ini
[Service]
Type=oneshot
ExecStart=/opt/conveyor/.venv/bin/python
  /opt/conveyor/scripts/auto_maintain.py
  --clean-threshold 100 --keep 50
ProtectSystem=strict / ProtectHome=read-only
```

The timer-passed `--clean-threshold 100 --keep 50` is intentionally more
conservative than the CLI default of `--clean-threshold 30` (commit `84de8a6`
lowered the CLI default; the timer values predate that and are still safe).

**conveyor-maintain.timer:**

```ini
[Timer]
OnBootSec=5min / OnUnitActiveSec=1h
Persistent=true / RandomizedDelaySec=5min
```

---

## 4. Telegram bot surface

| Command | Sandbox | Goes through codex? | Notes |
|---|---|---|---|
| `/run <prompt>` | danger-full-access | yes | chat-first default |
| `/fix <prompt>` | danger-full-access | yes | alias; merges via `/apply` |
| `/memo [cat] <x>` | n/a | **no** | fast path, see §6.3 |
| bare `记 x` | n/a | **no** | fast path, regex matched in bot.py |
| `/memory [cat]` | n/a | no | read today's MEMORY.md |
| `/apply <job_id>` | n/a | no | merge worktree back to main, only if main clean |
| `/cancel` | n/a | no | terminate running codex process |
| `/status` | n/a | no | current or last job summary |
| `/help` | n/a | no | usage |
| (plain text) | danger-full-access | yes | alias of `/run` |

User-locked: `_int_env('TELEGRAM_ALLOWED_USER_ID')` gates every handler.
1200-char message truncation; typing indicator while a job runs.

Telegram output runs through `redaction.redact_text` before send so
`/home/ubuntu/.env` keys and similar strings cannot leak via logs.

---

## 5. Codex CLI bridge

### 5.1 The single spawn point

`CodexRunner.start(mode, prompt, on_progress)` (file `runner.py`, class at
line 155, spawn sequence starts at `_run_job` ~line 470) is the only place
the runner shells out to codex. The exact command line:

```bash
codex exec --json --sandbox danger-full-access \
  --cd <worktree-path> \
  --add-dir <RUNNER_HOME> \
  --output-last-message <final-message-path> \
  [--model <model>] \
  -
```

Prompt goes to **stdin**, JSONL events come back on **stdout**, the final
assistant message lands in the file pointed to by `--output-last-message`.

`--add-dir <RUNNER_HOME>` is a **sandbox filesystem mount**, not a tool
registration — the LLM uses it to access the runner CLI
(`$CODEX_RUNNER_HOME/.venv/bin/python -m runner memorize ...`) when inside
a `/fix` job.

### 5.2 The payload assembly

`runner.py:477`:

```python
payload = (job.mode.stdin_prefix + self._prefetch_memory(job) + job.prompt).encode("utf-8")
```

`_prefetch_memory` returns `memory_context_text + tool_registry_text`. The
prefetch is the LLM's view of:

1. Today's MEMORY.md as `<memory-context>` (so the model sees prior
   `fact`/`preference`/etc. and can dedup decisions)
2. The `<tool-registry>` block describing what runner-CLI tools exist in
   the current sandbox

### 5.3 The tool-registry gate (the design decision that matters)

The runner has no real tool-call parser. `_tool_registry_text` (lines
659-724) is a **text prompt** injected into the LLM. The LLM is told which
runner CLI commands exist, told to invoke them via `cd "$CODEX_RUNNER_HOME"
&& .venv/bin/python -m runner memorize [...] "<content>"`, and that's it.
There is no JSON-schema validation, no parse layer, no per-tool policy.

In RUN/FIX (danger-full-access) the block lists shell, memorize, recall,
and other runner CLI tools the operator can invoke from chat. Narrowing
sandbox scope (e.g. back toward workspace-write) is future hardening —
not current behavior.

Legacy note: older docs described read-only RUN vs workspace-write FIX;
the sandbox is now unified as danger-full-access for single-operator VPS use.
The `memorize` policy is three-tier:

- `fact` — the LLM MAY auto-invoke when something is objectively true and
  verifiable (close price, server IP, tool behaviour)
- `preference` / `convention` / `tool-quirk` — only when the user EXPLICITLY
  asks ("记住...", `/memo ...`, etc.). Do not infer.
- `unfiled` — safe landing when the category is unclear. 12pm cron will
  reclassify unfiled entries via the runner's classifier.

This is the **load-bearing contract** between the runner and the LLM. The
secrets warning at the end of the block ("never write API keys into
MEMORY.md, runner has a redaction layer, don't rely on it") is the only
thing keeping `.env` values from leaking into a committed file.

---

## 6. Memory system

### 6.1 Categories

Five sections in MEMORY.md, in this order:

```
## preference
## fact
## tool-quirk
## convention
## unfiled
```

The first four are user-tagged (preferred); `unfiled` is the fallback.
Sections are matched verbatim by the reader and writer.

### 6.2 Where MEMORY.md lives

In the per-day worktree, e.g.
`/srv/conveyor/worktrees/day-2026-06-04/MEMORY.md`. The
`worktrees/day-YYYY-MM-DD` is shared across the day's jobs. The same file
is in scope for `git apply` (when `/apply` brings tracked changes back to
main) but MEMORY.md is **excluded** from that apply via pathspec
`:(exclude)MEMORY.md` — the worktree is ephemeral, MEMORY.md is the day's
running notes and the curator archives it to `~/.codex/JOURNAL/YYYY-MM-DD.md`
at the 12pm gate.

### 6.3 The fast path (the one that bypasses codex)

`bot.py:_handle_memo_fast_path` (lines 73-100) handles `记 x` and `/memo`
**without** spawning codex. It:

1. Strips the leading keyword via `MEMORY_KEYWORD_PATTERN`
2. Looks for an inline `[category]` tag; if present, uses it; otherwise
   calls `runner.classify_memo`
3. `classify_memo` is **never-raises** — any failure (no key, network,
   bad JSON, timeout) returns `"unfiled"`
4. Calls `runner.append_memo(category, content, auto_timestamp=...)`
5. `auto_timestamp=True` only for `fact`; other categories only stamp
   when the user explicitly asks ("记住...")
6. `append_memo` does file-wide dedup (not just the target section) —
   normalised line comparison skips the second write of a duplicate.
   This is what killed the "TSLA close $248 written 4 times" failure
   mode (commit `5de66bb`).
7. Reply is `记下了: <category> · <preview>` or `已存在: <category> ·
   <preview> (跳过重复)`.

The whole point of the fast path is **no codex call, no token cost, no
latency**, for the most-frequent command.

### 6.4 The 12pm gate

`reclassify_unfiled` is called at the user-local 12:00 gate from
`auto_maintain.py` and `compress_day.py`. It walks every `- ...` line in
`## unfiled` and re-invokes `classify_memo`. Lines the classifier still
cannot place stay in `## unfiled`.

This is the **safety net** for:

- LLM misclassifications in real time
- Items the user dropped into `unfiled` directly (e.g. via the CLI with
  `--category unfiled`)

### 6.5 The runner CLI surface

`python -m runner <subcommand>`:

| Subcommand | Purpose |
|---|---|
| `memorize [--category <c>] [--quiet] "<x>"` | append a memo, dedup, optional timestamp |
| `recall-memory [category]` | read today's MEMORY.md (full or one section) |
| `recall-journal <YYYY-MM-DD> [category]` | read a past day's archived journal |
| `classify-memo "<x>"` | the LLM classifier (CLI form) |
| `reclassify-unfiled` | the 12pm gate as a one-shot CLI |
| `submit` etc. | the operator CLI see §9 |

`runner.py:1264+` is where these are wired up.

---

## 7. Maintain pipeline (hourly)

`scripts/auto_maintain.py` runs every hour via the systemd timer. Order
of operations:

1. **`run_security_audit`** — `.env` permissions, repo token literals,
   recent service logs, systemd hardening. Never prints secrets.
2. **`backfill_job_metadata`** — for old job dirs missing `job.json`,
   generate one. Idempotent, `force=False`.
3. **Health snapshot** — two flavours:
   - `fast` (no offline harnesses, no security): `/srv/conveyor/health/latest-fast.json`
   - `full` (with offline + security): `/srv/conveyor/health/latest-full.json`
4. **GC** — `runner.clean_old_jobs(keep)` and
   `runner.clean_old_worktrees(keep_days=7)`. Fires only when **either**
   log count or worktree count ≥ `clean_threshold`. The timer passes
   `--clean-threshold 100 --keep 50`; the CLI default is 30 (commit
   `84de8a6` lowered it because 17 worktrees/day is the typical daily
   baseline).
5. **`compress_if_needed`** — at the 12pm user-local gate, archive
   yesterday's MEMORY.md to `~/.codex/JOURNAL/YYYY-MM-DD.md`. The 12pm
   gate is per-user-local timezone (`USER_TIMEZONE`,
   `America/Toronto`).
6. **Summary** — `MaintenanceOutcome` with `code=0` if all checks passed,
   `code=1` otherwise. The summary string is built from action lines,
   failed-check lines, and optional `triage_lines` advice.

**Important** (the bug we hit): the original `run_maintenance` had
`compress_if_needed` un-awaited in a list-comprehension
(`actions.append(compress_if_needed())`) that fed `\n".join(actions)`. The
coroutine object ended up in the joined string, the `join` raised
`TypeError: sequence item 1: expected str instance, coroutine found`, the
service exited 1, and there was no `OnFailure=` notify path. Fix is in
`2a81056` ("await compress_if_needed to keep timer out of failed state");
the 13:25-14:22 silent window is recorded in CHANGELOG "Honest gaps".

---

## 8. Smokes (93 cases, 8 scripts)

Local pre-deploy gate:

```bash
cd conveyor
make smoke                  # 8 env-free scripts, fast
make smoke-all              # also memo_smoke (requires .env with keys)
```

VPS deploy gate is per-script `python scripts/*_smoke.py` (the Makefile is
intentionally NOT in `deploy.sh`'s rsync list). The chain, in the order
Makefile declares them:

1. `auto_maintain_smoke` — the hourly harness, await-regression guard
2. `compress_day_smoke` — 5 branches + await guard
3. `clean_worktrees_smoke` — GC selection logic for old daily worktrees
4. `clean_old_jobs_smoke` — GC selection logic for old per-job log dirs
5. `classify_memo_smoke` — never-raise + return-string contract
6. `memo_flow_smoke` — append_memo + reclassify_unfiled contract
7. `memo_fastpath_smoke` — `_handle_memo_fast_path` routing contract
8. `progress_smoke` — chat-feel contract (19 -> 23 cases after
   `a6e0b09` round 2; 23 -> 26 cases after `c70de25` round 4;
   26 -> 30 cases after `6f1d9ea` round 5; 30 -> 32 cases after
   `57fd8aa` round 7; 32 -> 36 cases after `ddd468a` round 6;
   36 -> 39 cases after `edd2750` round 8;
   39 -> 42 cases after `54f1144` onboarding round;
  42 -> 44 cases after `484c085` onboarding C round;
  44 -> 46 cases after `b4540d5` hot-reload round;
   4 round-2 cases pin `command_execution` shell indicator,
   lifecycle suppression, no-event-type-prefix, and consecutive-
   same-text dedup). Round 3 (`0d76a15`) updates two of those cases
   to pin the binary-name extraction (`🔧 curl...` / `🔧 true...`
   instead of `🔧 shell...`); the `🔧 shell` fallback for empty /
   unparseable commands is asserted in the same case as a regression
   guard. Round 4 (`c70de25`) adds a per-item growing gate on prose
   events (1 AST + 2 behavior cases pinning `_is_prose_event`
   classification and the mid-stream shrink suppression). Round 5
   (`6f1d9ea`) adds a per-chain "💭 thinking..." indicator for
   sustained reasoning (4 behavior cases pinning the indicator
   firing after threshold, clearing on prose, clearing on tool
   call, and skipping for short-reasoning chains below the
   threshold). Round 7 (`57fd8aa`) adds a no-op edit guard (2
   behavior cases pinning the `last_progress_text` short-circuit
   for byte-identical text and for post-truncation collisions).
   Round 6 (`ddd468a`) adds a periodic tool-pulse while a tool
   call is in flight (4 behavior cases pinning the pulse firing
   after threshold, clearing on completion, skipping for short
   calls, and respecting the re-fire interval)
   Round 8 (`edd2750`) adds a first-event cooldown bypass (3
   behavior cases pinning the first prose firing immediately at
   the production-like 3.0s cooldown, the second event within
   the cooldown being gated, and the second event after the
   cooldown elapsing firing normally — the bypass is a one-shot
   seed of `_read_jsonl_stdout`'s `last_sent` at
   `-telegram_progress_seconds` so the first edit lands at T+0
   instead of waiting 3s for the shared cooldown to elapse)
   Onboarding (`54f1144`) adds the always-on operator profile
   + first-of-day day-brief contract (3 behavior cases pinning
   that the profile block is prepended to every prefetch with
   the 4 attrs and prose body, that the day-brief fires once
   on the first job of the day with all 3 sections and writes
   the state file, and that the day-brief is suppressed on the
   second job of the same day so the recap is not repeated on
   every message)
   Onboarding C (`484c085`) adds the load_settings persistence
   contract (2 behavior cases pinning that operator.json
   overrides .env for all 4 operator_* fields when the file
   exists, and that load_settings falls back to the .env values
   plus the project default for the unset field when the file
   is absent — the bot-side /onboard ConversationHandler and
   first-run nudge are deploy-gated on the VPS, not env-free
   smoke-tested)
   Hot-reload (`b4540d5`) adds 2 behavior cases pinning
   that `_operator_profile_text` re-reads operator.json
   fresh on every call (no SIGHUP, no bot restart). The
   first pins the env-fallback path when the file is
   absent; the second pins a 3-step live-edit sequence
   (no JSON -> env defaults; write JSON -> next call
   renders the new values; edit JSON -> next call
   renders the latest values). The runner.py change
   is a one-block swap: from `self.settings.operator_*`
   to `live.get(...) or self.settings.operator_* or
   default`. The settings.operator_* fields stay the
   env fallback; the JSON file is the live override.
   Onboarding C button (`0c13cdc`) is UI plumbing with 0
   new smoke cases — the button is a `CallbackQueryHandler`
   driving the same `onboard_start` entry point, which the
   existing onboard_* test handles already cover, and the
   VPS deploy gate verifies the live click. The feature is
   pinned in the project dossier via the round-8 / round-A+B
   / round-C pattern: each round adds prose to the snapshot
   header description, and the smoke count only bumps when
   a new behavior case lands.

`memo_smoke.py` is the full integration smoke and needs a populated `.env`
— it is gated behind `make smoke-all` precisely so the env-free chain is
fast and CI-friendly.

---

## 9. Operator CLI

```bash
# run a job from the VPS shell (no Telegram)
python scripts/submit_job.py --mode run|fix "<prompt>"
python scripts/submit_job.py --mode fix "<prompt>" --no-notify
```

`--no-notify` skips the Telegram progress callback path. The script
async-polls until the job id changes from a sentinel, returns rc=0 on
`COMPLETED`, rc=1 otherwise. Use this for cron jobs and manual debugging
when you don't want to fire off a Telegram message.

---

## 10. Configuration (config.py)

All env vars, with defaults:

| Var | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | bot identity |
| `TELEGRAM_ALLOWED_USER_ID` | yes | — | single-user lockout |
| `CODEX_WORKSPACE_ROOT` | yes | — | the user's repo on the VPS |
| `CODEX_BIN` | no | `codex` | path to codex CLI |
| `CODEX_TASK_ROOT` | no | `<workspace>/../conveyor` | where the runner lives |
| `CODEX_MODEL` | no | unset (codex default) | optional model override |
| `CODEX_TIMEOUT_SECONDS` | no | `3600` | per-attempt codex timeout |
| `TELEGRAM_PROGRESS_SECONDS` | no | `20` | typing-indicator tick |
| `CODEX_RETRY_429_DELAYS_SECONDS` | no | `300,900,1800` | comma-separated seconds, retry whole attempt on 429 |
| `CODEX_MEMORY_ROOT` | no | `~/.codex` | the curator's state dir |
| `USER_TIMEZONE` | no | `America/Toronto` | drives the 12pm gate |

`load_settings` creates `JOURNAL/`, `snapshots/`, `state/` under
`CODEX_MEMORY_ROOT` if missing. `.env` permissions should be `chmod 600`.

---

## 11. The Codex CLI state (under CODEX_MEMORY_ROOT)

The Codex CLI itself owns a parallel state directory (default
`/home/ubuntu/.codex/`). The runner only touches:

- `JOURNAL/YYYY-MM-DD.md` — the curator writes here at 12pm
- `snapshots/<YYYY-MM-DD>-<HHMM>/` — daily snapshots (timestamped
  subdir, **not** an in-place file)

Everything else is Codex CLI internal (the runner does not depend on
or manage):

- `state_5.sqlite` (+ -wal, -shm) — the live state DB; on VPS this is
  still in WAL mode at last check (1.5MB unwritten WAL)
- `logs_2.sqlite`, `goals_1.sqlite`, `memories_1.sqlite`
- `sessions/YYYY/MM/DD/...` — rollout JSONLs (30+ per active day)
- `skills/` — 5 default skills
- `.tmp/plugins/` — 173 entries of bundled plugin cache (normal)
- `shell_snapshots/` — codex shell snapshots
- `config.toml` — codex config; the runner does not write to it

---

## 12. File / line anchors (jump-to points)

| File:line | What |
|---|---|
| `runner.py:155` | `class CodexRunner` (1415-line monolith) |
| `runner.py:477` | payload assembly: `stdin_prefix + _prefetch_memory + prompt` |
| `runner.py:586-603` | `_codex_command` (the codex CLI line) |
| `runner.py:659-724` | `_tool_registry_text` (the design-contract block) |
| `runner.py:687-695` | the memorize shell-line hint given to the LLM |
| `runner.py:851-902` | `reclassify_unfiled` (12pm gate body) |
| `runner.py:997` | `async def classify_memo` |
| `runner.py:1264+` | CLI subcommands (`memorize`, `recall-memory`, ...) |
| `bot.py:14` | imports (`CodexRunner`, `JobMode`, `SecretRedactingFilter`) |
| `bot.py:36-50` | `SecretRedactingFilter` |
| `bot.py:55` | `runner = CodexRunner(settings)` single instance |
| `bot.py:73-100` | `_handle_memo_fast_path` (bypasses codex) |
| `scripts/auto_maintain.py:60-100` | `run_maintenance` body |
| `scripts/auto_maintain.py:80-87` | the summary `join` that the `2a81056` fix changed |
| `scripts/deploy.sh` | the rsync + restart deploy |
| `systemd/conveyor-maintain.timer` | hourly schedule |

---

## 13. Design rationale

### 13.1 Why text-prompt tool registry, not parsed tool calls

Hermes (Nous Research) and the OpenAI function-calling spec both want
`<tool_call>{...}</tool_call>` blocks, parsed + schema-validated + dispatched
to in-process Python. The runner does not do that. The runner tells the
LLM "here are the runner-CLI commands that exist; invoke them via the
shell tool" and lets codex CLI do the parsing and execution.

**Why this is fine for our scale:**

- One operator, one Telegram chat, fixed tool set of 4-5 commands
- The fast path (`/memo` and `记 x`) bypasses codex entirely, so the
  most-frequent command is unaffected by tool-routing concerns
- The codex CLI is the actual executor; the runner.py layer is just I/O
  plumbing around it
- 47 smoke cases all pass; we have a working feedback loop

**Where this would bite us:**

- Per-tool metrics (we cannot easily attribute failures to `memorize` vs
  `recall_memory` vs `shell`)
- Per-tool policy (e.g. "memorize is idempotent, shell is rate-limited and
  audited")
- Multi-tool per turn (we cannot ask the model "call `memorize` and
  `recall_memory` in parallel")
- Smokes that exercise the tool-routing path must run a real codex CLI
  subprocess, which is slow and brittle in CI

The Hermes-style refactor would cost 350-500 lines of new
`RunnerToolDispatcher` plus a partial smoke rewrite. The single trigger
that has actually been hit ("memorize shell silently failed and the model
re-wrote the same memo 4 times") was fixed at the runner.py level via
file-wide dedup (`5de66bb`) — without leaving the text-prompt model. So
**the architecture is right but the timing is wrong** for the refactor;
revisit when one of the three triggers in the CHANGELOG "Honest gaps"
actually becomes a real problem.

### 13.2 Why per-day worktrees

- One git worktree per UTC day, shared by all jobs that day, reduces
  churn (otherwise codex would have to deal with worktree creation and
  teardown on every message)
- The day-boundary is a natural archival seam (curator moves yesterday's
  MEMORY.md to `JOURNAL/` at 12pm user-local)
- Worktree GC is one knob (`--keep-days=7`) with a default that matches
  the typical "a week of recent jobs is fine" intuition

### 13.3 Why `fact` auto-timestamps but others don't

- `fact` is by definition time-stamped real-world data ("TSLA close $248
  on 2026-06-04"). Without a timestamp the data is useless.
- `preference` / `convention` / `tool-quirk` are durable; a timestamp on
  them is noise. The user opts in with "记住..." if they want the
  provenance.

### 13.4 Why maintain is a separate unit

A maintain crash must not take the bot down. The bot is interactive; the
maintain is housekeeping. Two separate units = two independent failure
domains = the user can still send `/run` and `/memo` even if GC is broken.

### 13.5 Why we don't have `git push`

There is no upstream. This repo is local-only; the VPS is a runtime, not
a clone. The "deploy" verb in this project means `deploy.sh`, not `git
push`. If you want a remote (e.g. GitHub backup), set one up; it would
not change the deploy model.

---

## 14. Honest gaps (active)

Inherited from CHANGELOG.md, expanded with current state:

- **API key rotation** — single key in `.env`, no rotation mechanism.
  If the key is leaked the bot is exposed until manual rotation.
- **Interactive approval buttons** — no Telegram inline-keyboard approvals
  for sensitive operations. The safety model is regex rejection +
  sandbox.
- **`/memo_edit`** — manual file edit + 12pm unfiled reclassify is the
  only path. The MEMORY.md file is regular markdown and is safe to
  hand-edit; the next `classify_memo` call will not touch a line it did
  not write.
- **Hermes-agent-style parsed tool-call routing** — see §13.1.
- **Channel decouple (Telegram + Feishu)** — duplicate memo/command logic
  in `bot.py` vs `feishu_bot.py`; harness is Telegram-shaped only. Design:
  `docs/003-channel-decoupling.md` (P0: extract `handlers/`, shared dispatch).
- **Maintain-failure alerting** — the 13:25 incident on 2026-06-04 had a
  57-minute silent window because the maintain unit has no
  `OnFailure=` notify path and no failed-run counter. The fix is in
  `2a81056`; the alerting gap is open.

---

## 15. Open / parked items (not actively worked)

These have been noted in prior sessions but not picked up. Pick one and
say so if you want it moved to active:

- AAPL 2026-06-04 close price (Toronto 16:00 = 20:00 UTC; queryable on
  demand)
- `/home/ubuntu/.codex/{state,snapshots,...}` audit (C-option read-only
  walk; bounded)
- 13:25 incident's introducing commit (the await bug fix in `2a81056`
  addresses the symptom; the question "which commit introduced the
  un-awaited coroutine" is open)
- Add a git remote (e.g. `origin` on GitHub) for backup
- AAPL / TSLA price snapshots tied to the 12pm gate
- Hermes-style tool-dispatch refactor (see §13.1)
- API key rotation, approval buttons, `/memo_edit` (bigger pieces, not
  small)

---

## 16. Conventions

- **Commit message style:** `<area>: <one-line summary>`, lowercase,
  imperative ("runner: plumb RUNNER_HOME into codex sandbox").
  Multi-line bodies are rare; keep one commit per logical change.
- **Author/committer:** `mammut001 <mammut001@users.noreply.github.com>`.
- **Branch:** `main`, no remote, no PRs. Linear history is fine.
- **Smoke first, deploy second.** A commit that breaks `make smoke`
  does not get deployed. Smoke scripts are part of the commit that
  changes the unit under test — never land a refactor without a smoke
  for the refactor's invariant.
- **Never `rsync --delete`** against the VPS project root. The script
  preamble says this. Honour it.
- **Secrets never in MEMORY.md.** The runner has `redact_text` but it
  is a safety net, not a policy. The policy is "the LLM is told not to
  write keys, and the human does not paste them in chat".
- **One thing at a time.** Land a commit, report, wait. Do not batch
  multiple unrelated changes into one commit even if they are small.
- **Driver mode semantics.** The operator uses "continue but don't ask
  me for options" — pick the bounded next step from the parked list,
  do it, report. If the operator says "下一步" without context, it
  means "continue but don't ask". Stop in a clean state if the next
  step is unclear or risky.

---

## 17. Quick-start (one-liners)

> In the snippets below, `$REMOTE` is shorthand for your SSH target
> (e.g. `ubuntu@203.0.113.42`). The deploy script reads
> `CONVEYOR_REMOTE`; the one-liners assume you have exported
> the same value as a shell variable.

```bash
# $REMOTE = $CONVEYOR_REMOTE, e.g. export REMOTE=ubuntu@203.0.113.42

# local pre-deploy gate
cd conveyor && make smoke

# deploy to VPS (rsync + restart bot)
cd conveyor && bash scripts/deploy.sh

# first install from laptop (rsync + venv + systemd + configure_env)
cd conveyor && bash scripts/install-remote.sh

# deploy CHANGELOG (not in deploy.sh list)
rsync -avz conveyor/CHANGELOG.md \
  $REMOTE:/opt/conveyor/CHANGELOG.md

# ssh to VPS, check services
ssh $REMOTE \
  'systemctl status conveyor-telegram-bot.service \
   && systemctl list-timers conveyor-maintain.timer \
   && journalctl -u conveyor-maintain.service -n 5'

# run an operator job (no Telegram, VPS-side)
ssh $REMOTE \
  'cd /opt/conveyor && \
   .venv/bin/python scripts/submit_job.py --mode run "say ready"'

# capture a memo from VPS shell (fast path, no codex)
ssh $REMOTE \
  'cd /opt/conveyor && \
   .venv/bin/python -m runner memorize --category fact "<x>"'
```

---

*Last updated: 2026-06-05, America/Toronto. Snapshot at HEAD `b4540d5`.*
