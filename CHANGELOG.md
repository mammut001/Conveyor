# CHANGELOG

Trajectory snapshot at HEAD `490b288` (23 commits). README.md owns the
file/command reference; this file is the change history and current state
at a glance.

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
- Regex rejection + 1200-char message truncation; typing indicator while
  a job runs

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

### Smokes (7 scripts, 47 cases)
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


## Commit timeline (all 23, newest first)

```
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

VPS `main` is at the same HEAD; bot unit `active`; VPS smoke run 9/9 on
the memo_fastpath block, full chain 47/47 green locally.
