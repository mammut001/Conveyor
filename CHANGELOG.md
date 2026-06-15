# CHANGELOG

# Trajectory snapshot at HEAD `b4540d5` (47 commits). README.md owns the
file/command reference; this file is the change history and current state
at a glance.

## [unreleased] - onboarding C button (UX polish)

## [unreleased] - hot-reload for operator.json

The /profile edit path (and the manual `ssh ... rm/sed operator.json`
path) used to require a `systemctl restart conveyor-telegram-bot.service`
to take effect: `_operator_profile_text` was reading
`self.settings.operator_*` which is the frozen Settings object
populated once at startup. This round adds a per-call re-read so
any operator.json edit is picked up on the very next job
without a bot restart. The 4 attrs (name, language, style,
standing) now resolve in this order on every prefetch:
  1. live `codex_memory_root/operator.json` (per-call re-read)
  2. `settings.operator_*` (env / startup defaults from .env)
  3. project default (e.g. `(anonymous)` for name when neither
     of the above is set)
The `settings.operator_*` fields stay as the env fallback; the
JSON file is the live override. Reads are O(1) from the page
cache (file is ~200 bytes); the single-write `_save` in
bot.py is atomic on Linux (under PIPE_BUF) so the live read
never sees a partial write.

This is a hot-reload without a SIGHUP handler: the
file-watch happens on every call, so no signal plumbing
is needed. The simpler design avoids the SIGHUP-reload
complexity (re-creating the frozen Settings dataclass,
threading the reload through Application) for the same
end-user UX: edits take effect on the next job.

- `b4540d5` - runner: hot-reload operator.json on every prefetch (no bot restart needed)
  - `config.py` - promote `_load_operator_profile` to the
    public `load_operator_profile`. The single underscore
    was signalling 'internal' when only config.py used
    it; runner.py now calls it too, so it's no longer
    internal. Behavior unchanged; rename only.
  - `runner.py` - `_operator_profile_text` now reads
    operator.json fresh on every call via
    `config.load_operator_profile(self.settings.codex_memory_root)`.
    The 4 attrs are resolved live (operator.json) then
    fall through to settings.operator_* then to a
    project default. The settings.operator_* fields
    stay the env fallback; the file is the live override.
  - `scripts/progress_smoke.py` - 2 new contract cases
    (`_test_operator_profile_text_uses_env_when_no_operator_json`,
    `_test_operator_profile_text_picks_up_live_operator_json_edit`).
    The first pins the no-profile-yet path: env defaults
    fall through when operator.json is absent. The
    second pins the hot-reload path: a 3-step sequence
    (no JSON -> env defaults render; write JSON with
    Alice/ja/verbose/team-lead -> next call renders
    Alice; edit JSON to Bob/en -> next call renders
    Bob) without any bot restart between calls. Both
    pass; 44 -> 46 cases in progress_smoke; 91 -> 93 in
    the full chain.

The first-run `/onboard` flow used to be command-only: the user
had to type `/onboard` after reading the welcome message or the
first-message nudge. This round adds a one-tap inline button
("开始 onboarding", `callback_data="ob:start"`) to both the
`/start` welcome and the `text_cmd` first-run nudge so the
user can launch the Q&A without typing. The button drives the
same `ConversationHandler` entry point as the `/onboard`
command (`onboard_start` is now callback-aware — it calls
`update.callback_query.answer()` before sending the first
question so Telegram dismisses the loading indicator).

Telegram bots cannot send proactive messages to users who have
not messaged them first; the button is the best UX in that
constraint. `/skip` remains available as a text command for
the "don't ask me anything, just use defaults" path.

- `0c13cdc` - bot: add inline '开始 onboarding' button to the first-run welcome (onboarding-C button)
  - `bot.py` - 4 small changes: `onboard_start` now handles
    both Update and CallbackQuery (calls
    `update.callback_query.answer()` to dismiss the button's
    loading indicator); `start_cmd` first-run reply includes
    the inline button via `InlineKeyboardMarkup`; `text_cmd`
    first-run nudge carries the same button; `ConversationHandler`
    `entry_points` extended with
    `CallbackQueryHandler(onboard_start, pattern=r"^ob:start$")`
    alongside the existing `CommandHandler("onboard", ...)`.
    The pattern anchor prevents a typo callback from another
    handler from accidentally entering the conversation.
  - 0 new smoke cases: the button is UI plumbing that
    env-free smoke cannot exercise end-to-end. The
    ConversationHandler shape is pinned by the existing
    onboard_* test handles; the VPS deploy gate verifies
    the live button click.

## [unreleased] - onboarding round C

The bot has been talking to the agent with no fixed identity and no
warm-start, and the operator has had no way to set that identity
short of editing .env on the VPS and restarting the bot. Onboarding
C is the first-run /onboard flow: a 3-step Q&A (name / language /
style) that writes a persistent `codex_memory_root/operator.json`
profile which load_settings reads at startup, overriding the .env
values. Hermes and similar personal AI agents run a similar
first-run experience (light-touch 3-5 questions, persistent
profile, editable later); the difference here is that the profile
file lives in `codex_memory_root` (not a dotfile in ~/) so the
bot's existing state-management surface stays the source of truth.

Round C's contract: the `/onboard` conversation runs as a
`ConversationHandler` with 3 states (ONBOARDING_NAME /
ONBOARDING_LANG / ONBOARDING_STYLE), uses inline keyboard buttons
for the language and style picks, accepts free text as a fallback
for any step, and writes the 4-field profile on completion. The
fallback paths are deliberate: `/skip` ends the conversation
without writing the file (the .env defaults stay in effect, the
user stays a "first-time user" until they come back to /onboard),
and a `bot.json` write failure is non-fatal (the conversation
ends with a "保存失败" message and the user can retry).

The loader side: `_load_operator_profile` in config.py reads
`operator.json` if it exists and returns the 4 known fields.
`load_settings` resolution order is `operator.json > .env >
dataclass default`, so the operator's explicit choice always
wins over the deployer's defaults. Restart required for changes
to take effect (this is a simplification; a follow-up round can
add hot-reload via a SIGHUP handler or a periodic file-watch).

The first-run nudge: both `start_cmd` and `text_cmd` detect the
missing `operator.json` and surface the /onboard prompt
immediately, instead of silently starting a job with the .env
defaults. A new `start_cmd` paragraph on a fresh install is the
canonical Hermes-style "first time" experience; a new
`text_cmd` line catches the case where the user typed something
before running /onboard (so the first message is not lost).

- `484c085` - bot: add first-run /onboard conversation + operator.json persistence (onboarding-C)
  - `bot.py` - 6 new handlers (onboard_start, onboard_name,
    onboard_lang_button, onboard_style_button, onboard_cancel,
    profile_cmd) + 3 conversation states (ONBOARDING_NAME /
    LANG / STYLE) + 3 helpers (_operator_profile_path,
    _operator_profile_exists, _save_operator_profile).
    ConversationHandler drives a 3-step Q&A: name (free text),
    language (3 inline buttons: zh-CN/en/ja), style (3 inline
    buttons: terse/balanced/detailed). `/skip` ends the
    conversation without writing operator.json. `/profile`
    shows the current 4-field profile and points at /onboard to
    re-run. `start_cmd` and `text_cmd` both nudge /onboard on
    the first-run path (no operator.json) instead of silently
    starting a job with the .env defaults. Imports extended:
    ConversationHandler, CallbackQueryHandler,
    InlineKeyboardButton, InlineKeyboardMarkup.
  - `config.py` - _load_operator_profile helper reads
    codex_memory_root/operator.json (if it exists) and returns
    a dict of overrides for the 4 operator_* fields.
    load_settings resolution order is now: operator.json > .env
    > dataclass default. Stale or unknown fields in the JSON
    are silently dropped (the loader only returns the 4 known
    keys) so a hand-edited or older profile file can't break
    load_settings. New import: json.
  - `scripts/progress_smoke.py` - 2 new contract cases
    (_test_load_settings_reads_operator_json_overrides,
    _test_load_settings_falls_back_to_env_when_no_operator_json).
    42 -> 44 cases in progress_smoke; 89 -> 91 in the full
    chain. The first pins persistence-wins (operator.json
    overrides .env for all 4 fields); the second pins the
    no-profile-yet path (.env values used; project default for
    unset fields). Test override pattern (mock.patch.dict for
    env, temp dir for memory_root) mirrors the round-5/6/8/C
    convention.

## [unreleased] - onboarding round A+B

The bot has been talking to the agent with no fixed identity and no
warm-start. Every Telegram message spawns a fresh codex subprocess
that re-discovers the operator's name, language, tone, and the
state of yesterday. Onboarding A+B gives the agent a stable
identity and a daily warm-up so the first message of the day does
not feel like a cold start.

Round A: an `<operator-profile>` block is prepended to every
prompt, carrying the 4 attrs the agent always needs to know (name,
language, style, standing). Values come from .env
(OPERATOR_NAME/LANGUAGE/STYLE/STANDING); the project defaults
match the single-operator / zh-CN / terse / personal-scale
assumption (anonymous, zh-CN, terse, personal-scale, single
operator). The block is always-on: it fires on /run, /fix, and the
plain-message path, with no per-mode branching. Empty
OPERATOR_NAME falls back to `(anonymous)` so the name attr is
never empty.

Round B: a `<day-brief>` block is delivered once per user-local
day, on the first job only. It has 3 sections (yesterday's journal
preview at ~/.codex/JOURNAL/YYYY-MM-DD.md, today's MEMORY.md
preview, and the last 3 jobs' summaries from logs/). State is a
one-line date stamp at codex_memory_root/state/last_day_brief.txt
written on the first deliver; subsequent jobs the same day get
'' (no brief) so the recap is not repeated on every message.
Failure to read or write the state file is non-fatal: a duplicate
brief is harmless; a missing brief is the cold start we are
trying to avoid. After the first send, the agent already has
yesterday's context and can lead with carryovers instead of asking
"what was I doing yesterday?".

- `54f1144` - runner: inject operator profile + first-of-day day-brief into every prompt (onboarding A+B)
  - `config.py` - 4 new Settings fields (operator_name/language/
    style/standing) loaded from .env (OPERATOR_NAME/LANGUAGE/
    STYLE/STANDING). Fields live at the END of the dataclass
    with defaults so existing Settings(...) positional callers in
    the smokes keep working unchanged. Defaults: None, zh-CN,
    terse, personal-scale, single operator.
  - `.env.example` - 4 commented lines documenting the new
    OPERATOR_* env vars, grouped with USER_TIMEZONE.
  - `runner.py` - 2 new methods (`_operator_profile_text`,
    `_day_brief_text`) + 3 class constants
    (DAY_BRIEF_STATE_FILENAME/PREVIEW_CHARS/RECENT_JOBS) + 2
    helpers (`_day_brief_state_path`,
    `_day_brief_recent_jobs`). `_prefetch_memory` rewired to
    call 4 blocks in order: profile, day-brief, memory-context,
    tool-registry. The profile block is mode-agnostic and
    always-on; the day-brief is one-shot per user-local day.
  - `scripts/progress_smoke.py` - 3 new contract cases
    (`_test_operator_profile_block_in_prefetch`,
    `_test_day_brief_fires_on_first_job_of_day`,
    `_test_day_brief_skipped_on_second_job_of_day`); 39 -> 42
    cases in progress_smoke. The first pins that the profile
    block has all 4 attrs and a prose body, and is prepended
    before `<memory-context>`. The second pins the first-of-day
    brief shape (3 sections, state file written with today's
    date). The third pins the one-brief-per-day contract
    (second call returns '' when the state file is dated today).
    Test override pattern (object.__setattr__ on frozen Settings,
    mock.patch.dict for env) mirrors the round-5/6/8 convention.

## [unreleased] - chat-feel polish round 8

Round 8 plugs the last chat-feel gap that rounds 5/6/7 left visible:
after the bot's sub-second placeholder landed, the first prose edit
had to wait 3 seconds (= `telegram_progress_seconds` default) before
passing the `now - last_sent >= telegram_progress_seconds` cooldown
shared by all 3 gates in `_read_jsonl_stdout` (thinking indicator,
prose, tool-pulse). The placeholder sat at "⏳ Got it, working on
it..." for 3s before the first edit, so the chat looked frozen vs
Hermes-style "first prose appears immediately". Round 8 seeds
`last_sent` at `-telegram_progress_seconds` instead of `0.0`, so
the first event passes the cooldown (`now - (-3.0) >= 3.0` is
always true for `now >= 0`). After the first send, `last_sent` is
updated to `now`, so the normal cooldown applies for the rest of
the stream. This is a one-shot bypass, not a permanent lowering of
the cooldown. The thinking indicator still fires after
`THINKING_THRESHOLD_SECONDS` (1s) of sustained reasoning, and the
tool-pulse still arms at `TOOL_PULSE_THRESHOLD_SECONDS` (4s) and
re-fires at `TOOL_PULSE_INTERVAL_SECONDS` (4s) — same as before.

- `edd2750` - runner: bypass progress cooldown for the first event after the placeholder
  - `runner.py` - `last_sent` in `_read_jsonl_stdout` now initialized
    to `-self.settings.telegram_progress_seconds` instead of `0.0`.
    The 3-gate chain (thinking indicator at `:871`, prose at `:881`,
    tool-pulse at `:890`) all gate by `now - last_sent >=
    telegram_progress_seconds`, so the first event had to wait 3s
    after the loop started (= 3s after `_start_job` called
    `runner.start`) before passing the cooldown. The placeholder
    appeared at T+0 sub-second but the first edit landed at T+3s,
    making the chat look frozen vs Hermes-style "first prose
    appears immediately". After the first send, `last_sent` is
    updated to `now` so normal cooldown applies for the rest of
    the stream. This is a one-shot bypass, not a permanent
    lowering of the cooldown. All 3 gates benefit automatically
    (they share the same `last_sent` cooldown)
  - `scripts/progress_smoke.py` - 3 new contract cases
    (`_test_first_prose_fires_immediately_after_placeholder`,
    `_test_subsequent_prose_respects_cooldown`,
    `_test_subsequent_prose_fires_after_cooldown`); 36 -> 39 cases.
    The first test pins the bypass at the production-like 3.0s
    cooldown (not zeroed, so the bypass is real, not just that the
    constant is wired in); the second pins that the bypass is
    one-shot (a 2nd event microseconds after the 1st is gated by
    the cooldown); the third pins that normal cooldown applies to
    2nd+ events (a 2nd event 3.1s after the 1st passes)

## [unreleased] - chat-feel polish rounds 6+7

Rounds 6 and 7 plug the last two chat-feel gaps that round 5 left
visible. After round 5, a long-running tool call (a `curl` of a slow
API, a multi-second `python` job, a `git` clone) still shows just
`🔧 curl...` once and then the placeholder sits silent for the
duration, so the chat looks frozen even though the model is alive.
Round 6 adds a periodic `🔧 name (Ns)...` tool-pulse every 4s while a
tool call is in flight: arm on `item.started`, disarm on the matching
`item.completed`, gated by threshold + interval so short calls and
frequent re-fires do not spam the chat. The other gap is a live bug:
Telegram's `editMessageText` 400s with `Message is not modified` when
the new content matches the current content, and the existing
`except` latch was flipping the rest of the job into `send_message`
mode, scattering one-off messages through the chat. Live evidence:
journalctl on the VPS showed 2 such 400s on 2026-06-05 18:09 and
18:10, both at `bot.py:574` (the edit call). Round 7 short-circuits
identical `progress()` payloads before the wire so the storm cannot
start. Together, round 6 surfaces a long tool call, round 7 keeps the
edit ladder intact when the model emits a no-op progression.

- `57fd8aa` - bot: skip no-op placeholder edits to prevent edit-broken latch storm
  - `bot.py` - `progress()` in `_start_job` now tracks `last_progress_text`
    (post-truncation, the wire format) and short-circuits when the next
    call's outgoing text matches. Telegram's `editMessageText` 400s
    with `Message is not modified` on identical content, and the
    existing `except` latch was flipping the rest of the job into
    `send_message` mode, scattering one-off messages through the chat.
    Live evidence: journalctl showed 2 such 400s on 2026-06-05 18:09
    and 18:10, both at `bot.py:574` (the edit call)
  - `scripts/progress_smoke.py` - 2 new contract cases
    (`_test_no_op_edit_skipped`,
    `_test_no_op_edit_skipped_after_truncation_collision`); 30 -> 32
    cases

- `ddd468a` - runner: surface periodic tool-call pulse while a tool call is in flight
  - `runner.py` - new module-level constants `TOOL_PULSE_THRESHOLD_SECONDS`
    (4.0s) and `TOOL_PULSE_INTERVAL_SECONDS` (4.0s). Re-binding these
    at the module level is the test override hook (mirrors
    `THINKING_THRESHOLD_SECONDS`)
  - `runner.py` - `_is_tool_call_start_event` / `_is_tool_call_complete_event`
    helpers (delegate to `_tool_call_name`). Arm fires on `item.started`
    (or `item.updated`) for a `function_call` / `tool_call` /
    `command_execution` envelope; disarm fires on `item.completed`
    with a name-match guard so a stale complete from a prior call
    cannot wipe a fresh arm
  - `runner.py` - `_read_jsonl_stdout` gains 3 per-stream state vars
    (`pending_tool_name` / `pending_tool_since` / `last_pulse_at`)
    and a 7th gate (pulse send) downstream of the existing 6-gate
    chain. Pulse text is `🔧 name (Ns)...`. Shares
    `telegram_progress_seconds` cooldown with the rest of the gate
    ladder. `last_sent_text` is NOT updated on the pulse so a
    tool-call summary that lands right after the pulse still surfaces
  - `scripts/progress_smoke.py` - 4 new contract cases
    (`_test_tool_pulse_appears_after_threshold`,
    `_test_tool_pulse_clears_on_completion`,
    `_test_tool_pulse_skipped_for_short_call`,
    `_test_tool_pulse_respects_interval`); 32 -> 36 cases

## [unreleased] - chat-feel polish round 5

Round 5 plugs the last chat-feel gap that round 4 left visible: when
the model enters a long reasoning burst (math, multi-step planning,
debugging chains), reasoning events stream silently via
`_event_summary` returning "" (runner.py:1406), so the placeholder
sits at the bot's initial "⏳ Got it, working on it..." for 5-30s and
the chat looks frozen. Round 5 surfaces a short "💭 thinking..."
indicator after 1.0s of sustained reasoning so the user knows the
model is alive. Any non-reasoning event (prose, tool indicator,
`item.completed`, lifecycle, malformed JSON) breaks the chain so the
next reasoning burst starts a fresh threshold window. The indicator
is sent at most once per chain and shares the existing
`telegram_progress_seconds` cooldown so the next prose is not
double-blasted. The contract is added in `_read_jsonl_stdout` after
the existing 5-gate chain; the next prose is still gated by the
round-4 per-item growing check.

- `6f1d9ea` - runner: surface sustained-reasoning thinking indicator after 1s threshold
  - `runner.py` - new module-level constants `THINKING_INDICATOR`
    (`"💭 thinking..."`) and `THINKING_THRESHOLD_SECONDS` (1.0).
    Re-binding `THINKING_THRESHOLD_SECONDS` at the module level is
    the test override hook (mirrors the frozen-Settings bypass
    used by progress_smoke)
  - `runner.py` - `_read_jsonl_stdout` gains two new per-stream
    state vars: `thinking_since: float | None` (start time of the
    current reasoning chain) and `thinking_indicator_sent: bool`
    (whether the indicator has already fired for this chain). After
    the existing `now = asyncio.get_running_loop().time()` line,
    chain management runs first: a reasoning event extends the
    chain (sets `thinking_since` on first event of the chain); any
    non-reasoning event breaks the chain (clears `thinking_since`
    AND resets `thinking_indicator_sent` so the next chain is
    eligible to fire again). A malformed JSON payload
    (`event_obj is None`) is treated as "unknown, bail out" and
    breaks the chain too
  - `runner.py` - `_read_jsonl_stdout` adds a 6th gate BEFORE the
    existing prose `if` block. It evaluates True when
    `thinking_since` is not None, `thinking_indicator_sent` is
    False, `now - thinking_since >= THINKING_THRESHOLD_SECONDS`,
    and the existing `telegram_progress_seconds` cooldown is
    satisfied. The send block updates `last_sent`,
    `last_sent_text = THINKING_INDICATOR`, sets
    `thinking_indicator_sent = True`, and calls
    `on_progress(truncate(THINKING_INDICATOR, 1200))`. The block
    comes BEFORE the prose `if` so the cooldown clock is shared
    correctly: if it came after, the prose block would have already
    updated `last_sent` and the indicator would not fire
  - `runner.py` - the raw reasoning line is still written to
    `job.log_path`; only the user-facing chat edit is gated. The
    `job.last_event` field is also NOT updated for reasoning
    events (the existing `if event_text and not ...reasoning...`
    guard already excluded reasoning)
  - `scripts/progress_smoke.py` - 4 new contract cases
    (`_test_thinking_indicator_appears_after_threshold`,
    `_test_thinking_indicator_clears_on_prose`,
    `_test_thinking_indicator_clears_on_tool_call`,
    `_test_thinking_indicator_skipped_for_short_reasoning`);
    26 -> 30 cases

## [unreleased] - chat-feel polish round 4

Round 4 plugs the last prose-streaming chat-feel gap that round 2 left
visible. The model can briefly re-write a paragraph mid-stream, which
made the placeholder visibly shrink to a shorter string (the chat
looked like the model was "going backwards"). Round 4 adds a per-item
"growing" gate: the placeholder only forwards edits that strictly
extend the last sent prose, `item.completed` is exempt so the final
text always wins, and the tracker resets on complete so the next
item can start a new growing chain. Tool-call indicators (🔧 curl...)
bypass the gate on purpose - the user wants the current state, not a
growing sequence. Lifecycle events were already filtered to "" by
`_event_summary` so they short-circuit before the new gate; the gate
is downstream of the existing 4-gate chain and adds a 5th.

- `c70de25` - runner: gate prose updates by length per item to avoid chat re-write
  - `runner.py` - new `_is_prose_event` helper. Strict subset of
    `_is_user_visible_event`: agent_message items and top-level
    text-like fields are prose; lifecycle, reasoning, function_call,
    and command_execution are NOT prose (the growing gate only
    applies to user-readable chat text)
  - `runner.py` - `_read_jsonl_stdout` adds a 5th gate in the
    existing chain. `last_prose_text` is updated only on prose sends
    (not tool indicators). The gate evaluates to True when the new
    event is non-prose, or is the first prose after a None tracker,
    or strictly extends the last sent prose, or is `item.completed`
    (which resets the tracker to None so the next item starts fresh).
    Mid-stream shrinks are now suppressed; the raw line is still
    written to `job.log_path`
  - `scripts/progress_smoke.py` - 3 new contract cases
    (`_test_is_prose_event_exists`,
    `_test_is_prose_event_classification`,
    `_test_growing_gate_on_prose`); 23 -> 26 cases

## [unreleased] - chat-feel polish batch

This batch is a chat-feel pass on the Telegram bot. The runtime surface
moves: the bot now acknowledges in <1s, edits the placeholder in place
as model prose streams in, surfaces tool calls as a short indicator,
and latches to a send-message fallback if Telegram rate-limits edits.

- `1d03c53` - chat-feel polish batch
  - `bot.py` - sub-second placeholder ("⏳ Got it, working on it..."),
    edit-in-place progress, `edit_broken` latch to stop retry storms
    when Telegram rate-limits `edit_message_text`
  - `bot.py` - typing-loop interval 4s → 1.5s so the chat-list pulse
    stays alive within Telegram's 5s auto-typing expiry
  - `runner.py` - `_tool_call_name` + `_is_user_visible_event` extension
    so `function_call` items surface as a "🔧 name..." progress line
    instead of leaving the placeholder frozen mid-tool
  - `config.py` / `.env.example` - default `TELEGRAM_PROGRESS_SECONDS`
    20s → 3s so the user sees prose growing in near-real-time
  - `scripts/progress_smoke.py` - new Tier 1 contract smoke
    (14 → 18 cases), wired into `make smoke`

## [unreleased] - chat-feel polish round 2

This round plugs the real-world codex event-shape holes that round 1
left visible in the chat. After round 1 the placeholder still flickered
to raw JSON for some event types and held the cooldown hostage to no-op
updates. Round 2 fixes three contract gaps in `_event_summary` /
`_read_jsonl_stdout` and pins them with four new smoke cases.

- `a6e0b09` - chat-feel polish round 2
  - `runner.py` - `_tool_call_name` now also matches `command_execution`
    items and falls back to `"shell"` (real codex `command_execution`
    items do not carry a `name`; round 1 JSON-dumped a multi-kilobyte
    curl command into the chat)
  - `runner.py` - `_event_summary` returns `""` for the five lifecycle
    event types (`thread.started` / `thread.completed` / `turn.started` /
    `turn.completed` / `turn.failed`); `turn.completed` usage is still
    captured separately via `_capture_usage`, so suppressing the
    summary here loses no data, and the cooldown clock no longer
    resets on a JSON dump
  - `runner.py` - `_event_summary` drops the `event_type:` prefix from
    prose, tool-indicator, and top-level text fields; opaque events
    (item with no agent_message text, no tool name, no top-level text
    field) now return `""` instead of dumping JSON, so the chat
    surface reads like a chat and the raw line stays in `job.log_path`
  - `runner.py` - `_read_jsonl_stdout` adds a consecutive-same-text
    dedup: when codex emits `item.started` + `item.completed` for the
    same tool call in quick succession, the second identical
    indicator is suppressed; the time-based cooldown stays
  - `scripts/progress_smoke.py` - 4 new contract cases
    (`_test_command_execution_tool_indicator`,
    `_test_lifecycle_events_suppressed`, `_test_no_event_type_prefix`,
    `_test_consecutive_dedup`); 19 -> 23 cases

## [unreleased] - chat-feel polish round 3

Round 3 plugs the last chat-feel gap that round 2 left visible: the
`command_execution` indicator said `"shell"`, which is technically
safe but tells the user nothing about what is actually running. A
real-world codex session surfaces lots of `command_execution` items
(`curl`, `python`, `git`, `bash`, ...) and seeing `🔧 shell...`
three times in a row reads as a frozen chat. Round 3 extracts the
leading binary name from the command string (handling the
`/bin/bash -lc '...'` wrapper, the `&&` chain, and the `|` pipe
source) and surfaces `🔧 curl...`, `🔧 python...`, etc. The raw
command body still does NOT leak - that was the original round-1
problem and the round-3 contract is strictly tighter: only the
binary name (path-stripped, capped at 32 chars) appears in chat.

- `0d76a15` - runner: extract actual binary name from command_execution indicators
  - `runner.py` - new module-level `_extract_command_name(command)`
    helper. Strips the `/bin/bash -lc '...'` wrapper if present,
    takes the last `&&` segment (the effective command in a chain),
    then the first part of any `|` pipe (the source), then the first
    whitespace-delimited word, and finally `Path(name).name` to
    drop the path. Returns None on empty / unparseable input so the
    caller falls back to the existing `"shell"` indicator.
  - `runner.py` - `_tool_call_name` consults `_extract_command_name`
    before returning the `"shell"` fallback. The new contract is
    `🔧 <binary>...` for parseable commands and `🔧 shell...` for
    empty / missing / unparseable ones.
  - `scripts/progress_smoke.py` - `_test_command_execution_tool_indicator`
    updated to expect `🔧 curl...` for a `curl ...` command and to
    assert command-body needles (`example.com`, `-w`, `http_code`,
    `/dev/null`) do NOT appear; the original `🔧 shell` contract
    is preserved as a fallback for empty / whitespace / missing
    `command` field, so the indicator is never vague or empty.
  - `scripts/progress_smoke.py` - `_test_consecutive_dedup` updated
    to expect `🔧 true...` (its `command: "true"` payload now
    extracts to `true` instead of falling back to `shell`). The
    dedup contract (2 raw lines, 1 `on_progress` call) is
    unchanged.
- `04fbced` - compress-day-smoke: freeze clock for day-boundary branches
  - `scripts/compress_day_smoke.py` - wrap the three `compress_if_needed`
    calls that pin a specific `today` (`_test_already_ran_today`,
    `_test_no_prior_day`, `_test_last_covers_candidate`) in
    `_frozen_clock(today)` so the day-boundary branch asserts the
    contract on the intended day, not on whatever day the test
    host happens to be on. Surfaces only on hosts whose wall clock
    has advanced past 2026-06-04. Orthogonal to chat-feel; landed
    in its own commit per the "one thing at a time" rule.

## [unreleased] - open-source prep batch

This batch is governance, docs, and CI only; the runtime surface
("Current surface" below) is unchanged at HEAD `490b288`.

- `e899a32` - add `LICENSE` (MIT), `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, and a GitHub Actions smoke gate
  (`.github/workflows/smoke.yml`)
- `490b288` - scrub VPS IP and local paths from the repo, add a
  "How it works" callout to `README.md`
- `2b59622` - relocate project root from `~/Desktop/Focus/` to
  `~/Documents/GitHub/telegram_codex_runner/`

## Current surface

### Telegram bot (bot.py)
- `/run` (read-only codex), `/fix` (workspace-write codex)
- `/memo` and bare `记 x` (fast path that writes to today's MEMORY.md
  without spawning codex)
- `/memory [category]` (read), `/apply <job_id>` (merge a /fix result back)
- `/cancel`, `/status`, `/help`
- Regex rejection + 1200-char message truncation
- Chat-feel: sub-second "⏳ Got it, working on it..." placeholder,
  edit-in-place progress as model prose streams in, "🔧 tool..." indicator
  for `function_call` items (and `command_execution` items surface as
  `🔧 <binary>...` where the binary name is extracted from the command
  string, with a `🔧 shell...` fallback for empty / unparseable commands),
  "🔧 name (Ns)..." periodic tool-pulse every 4s while a tool call is
  in flight (round 6; arm on `item.started`, disarm on matching
  `item.completed`, gated by threshold + interval so short calls and
  frequent re-fires do not spam the chat),
  "💭 thinking..." indicator after >1.0s of sustained reasoning so a
  hard think (math, multi-step planning, debugging) does not look
  frozen (round 5; per-chain, fires at most once, breaks on any
  non-reasoning event), typing pulse every 1.5s for the job's
  lifetime; skips no-op placeholder edits (round 7; the 2nd identical
  `progress()` short-circuits before the wire so `BadRequest: Message
  is not modified` cannot trip the edit-broken latch); latches to
  send-message if Telegram rate-limits edits; the first prose edit
  lands immediately after the placeholder (round 8; `last_sent` in
  `_read_jsonl_stdout` is seeded at `-telegram_progress_seconds`
  instead of `0.0`, so the first event passes the shared cooldown
  without waiting 3s; after the first send, `last_sent` is updated
  to `now` so the normal cooldown applies for the rest of the
  stream; this is a one-shot bypass, not a permanent lowering of
  the cooldown, and all 3 gates (thinking indicator, prose,
  tool-pulse) benefit automatically since they share the same
  `last_sent`)

### Codex CLI bridge (runner.py)
- `CodexRunner.start(mode, prompt, on_progress)` is the single spawn point
- `--mode` resolves to exactly three things: `--sandbox` flag, the
  one-line `stdin_prefix` hint, and the `<tool-registry>` block
- The codex CLI line:
  `codex exec --json --sandbox <sandbox> --cd <worktree>`
  `--add-dir <RUNNER_HOME> --output-last-message <path> [--model <m>] -`
  (prompt goes to stdin)
- 429 retry loop driven by `codex_retry_429_delays_seconds`
- Per-day worktree (`worktrees/day-YYYY-MM-DD`) is shared across the day's
  jobs; MEMORY.md in that worktree is the day's running notes and is
  excluded from the `git apply` that brings tracked changes back to main
- `_prefetch_memory` stitches 4 blocks before the user's prompt, in order:
  `<operator-profile>` (onboarding-A; always-on; 4 attrs from .env
  OPERATOR_NAME/LANGUAGE/STYLE/STANDING; defaults to anonymous / zh-CN /
  terse / personal-scale, single operator), `<day-brief>` (onboarding-B;
  one-shot per user-local day; 3 sections - yesterday's journal preview,
  today's MEMORY.md preview, last 3 jobs' summaries; state at
  codex_memory_root/state/last_day_brief.txt), `<memory-context>` (today's
  MEMORY.md), `<tool-registry>` (mode-aware tool list)

### Memory system (runner.py MEMORY.md)
- Five sections: `preference`, `fact`, `tool-quirk`, `convention`, `unfiled`
- `append_memo` (`python -m runner memorize [--category <c>] [--quiet] "<x>"`):
  dedup runs file-wide (not just the target section); omit `--category` to
  let `classify_memo` pick; auto-timestamp is on for `fact` only -
  preference / convention / tool-quirk only stamp when the user
  explicitly asks ("记住...")
- `classify_memo` (never-raises, returns the category string)
- `reclassify_unfiled` (called at the 12:00 user-local gate)
- `recall_memory` / `recall_journal` for retrieval
- No `/memo_edit` yet - manual file edit + the 12pm unfiled reclassify are
  the safety net for misfiled entries

### Tool-registry gate (runner.py `_tool_registry_text`)
- `<tool-registry>` block is injected into the codex prompt so the model
  sees exactly which runner-CLI tools this sandbox exposes
- RUN sandbox: "no shell, no writes, no runner CLI; web tools
  (search/fetch) ARE available so plain chat can answer current-info
  questions (prices, docs, news) without forcing /fix; ask the user
  to re-send as /fix only when writes or shell are needed"
- FIX sandbox: full `memorize` / `recall_memory` / `recall_journal` /
  `shell` surface with the three-tier auto-add policy spelled out
- Warns codex that `apply_patch` / `edit_file` / `write_file` are
  rejected by the router in this sandbox; all writes go through
  `python -m runner memorize`
- design (2026-06-05): /run mode now allows web tools (search/fetch) by
  default in natural-language chat. The codex CLI's read-only sandbox
  already permits web tools — the old "no network" wording in the
  prompt was the only thing blocking them. This is a 2-line prompt
  change (JobMode.stdin_prefix + _tool_registry_text RUN branch);
  writes and shell remain gated to /fix so the security boundary is
  preserved. New smoke assertion in `scripts/memo_smoke.py` pins the
  contract.

### Maintain pipeline (hourly systemd timer)
- `scripts/auto_maintain.py` runs every hour; GC fires when either the
  log count or the worktree count hits threshold
- `--clean-threshold` default is `30` (CLI; lowered from 100 in
  `84de8a6`). The systemd timer passes `--clean-threshold 100 --keep 50`
  explicitly - the timer is intentionally more conservative than manual
  CLI runs
- Calls `compress_if_needed` to archive yesterday's MEMORY.md to
  `~/.codex/JOURNAL/YYYY-MM-DD.md`
- 12:00 user-local gate reclassifies today's `unfiled` entries
- Maintain is a separate unit from the bot - a maintain failure does not
  take the bot down

### Smokes (8 scripts, 93 cases)
- `make smoke` is the local pre-deploy gate
- VPS deploy gate is per-script `python scripts/*_smoke.py`; the Makefile
  is intentionally NOT in `scripts/deploy.sh`'s rsync list
- Chain: `auto_maintain_smoke` then `compress_day_smoke` then
  `clean_worktrees_smoke` then `clean_old_jobs_smoke` then
  `classify_memo_smoke` then `memo_flow_smoke` then `memo_fastpath_smoke`

### Operator CLI (scripts/submit_job.py)
- `python scripts/submit_job.py --mode run|fix "prompt"` for direct
  CLI-driven jobs (cron, manual debugging)
- async-poll until job id changes; rc=0 on COMPLETED, rc=1 otherwise
- `--no-notify` to skip Telegram progress callbacks

## Honest gaps

- **API key rotation** - single key in `.env`; no rotation mechanism
- **Interactive approval buttons** - README has a "Later" section; the
  current safety model is regex rejection + sandbox
- **`/memo_edit`** - manual file edit + the 12pm unfiled reclassify is
  the only path
- **Hermes-agent-style parsed tool-call routing** - current design is
  "runner CLI via shell + injected tool-registry"; not a parsed
  function-call surface
- **Maintain-failure alerting** - on 2026-06-04 13:25 UTC the hourly
  `codex-telegram-maintain.service` exited 1 (unawaited
  `compress_if_needed` coroutine; "sequence item 1: expected str
  instance, coroutine found") and stayed failed ~57 min before the
  14:22 run recovered on its own. Fix is in `2a81056`; the gap is
  the silent window: no `OnFailure=` notify path, no failed-run
  counter, the next hourly tick is what surfaces the recovery


## Commit timeline (all 47, newest first)

```
b4540d5 runner: hot-reload operator.json on every prefetch (no bot restart needed)
0c13cdc bot: add inline '开始 onboarding' button to the first-run welcome (onboarding-C button)
484c085 bot: add first-run /onboard conversation + operator.json persistence (onboarding-C)
54f1144 runner: inject operator profile + first-of-day day-brief into every prompt (onboarding A+B)
edd2750 runner: bypass progress cooldown for the first event after the placeholder
ddd468a runner: surface periodic tool-call pulse while a tool call is in flight
57fd8aa bot: skip no-op placeholder edits to prevent edit-broken latch storm
6f1d9ea runner: surface sustained-reasoning thinking indicator after 1s threshold
c70de25 runner: gate prose updates by length per item to avoid chat re-write
04fbced compress-day-smoke: freeze clock for day-boundary branches
0d76a15 runner: extract actual binary name from command_execution indicators
db114df docs: refresh snapshot, timeline, and smoke count after chat-feel round 2
a6e0b09 runner: fix chat-feel for real-world codex events (command_execution, lifecycle, dedup)
1d03c53 chat-feel: placeholder + edit-in-place + tool indicator + fallback latch
490b288 open-source: scrub personal markers, add README architecture callout
e899a32 open-source: add LICENSE, governance, and CI smoke gate
2b59622 docs: relocate project from Desktop/Focus to Documents/GitHub
8f49c79 docs: fix CHANGELOG timeline SHA for 5b9a170
5b9a170 runner + smoke: allow web tools (search/fetch) in /run mode by default
7193602 docs: fix worktree root path in §3.1 and §6.2
7d55769 docs: note 13:25 maintain-failure alerting gap in Honest gaps
dbc718a docs: add CHANGELOG.md with current surface and 15-commit timeline
408f3b2 smoke: add memo_fastpath_smoke pinning _handle_memo_fast_path routing
e4f59ad smoke: add memo_flow_smoke pinning append_memo + reclassify_unfiled contracts
f3ae4a4 smoke: add classify_memo_smoke pinning never-raise + return contract
8319529 smoke: add clean_old_jobs_smoke covering job-log GC selection logic
5f5d8fc smoke: add clean_worktrees_smoke covering GC selection logic
84de8a6 auto_maintain: lower default --clean-threshold from 100 to 30
1dc3ada Makefile: chain auto_maintain + compress_day smokes for pre-deploy gate
66610ca scripts: add compress_day_smoke covering 5 branches + await guard
590e2f8 scripts: add auto_maintain_smoke for await-regression guard
2a81056 auto_maintain: await compress_if_needed to keep timer out of failed state
5de66bb memo: dedup cross-section in append_memo
e786a65 runner: plumb RUNNER_HOME into codex sandbox via --add-dir and CODEX_RUNNER_HOME
95974f9 tool-registry: warn model that apply_patch is unavailable; route all writes through runner CLI
2abd548 inject tool-registry into codex prompt; add runner CLI subcommands; fix append_memo category
2df91f7 cleanup JobMode.MEMO; reuse classify_memo in compress_day.py for unfiled reclass
```

VPS `main` is at the same HEAD; bot unit `active`; full chain 93/93
green locally across 8 env-free smoke scripts.
