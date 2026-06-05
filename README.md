# Telegram Codex Runner

Small Python service that lets one whitelisted Telegram user run `codex exec --json` jobs on an Ubuntu VPS.

## How it works

A single Python process runs on the VPS:

1. `bot.py` receives Telegram messages and gates them on
   `TELEGRAM_ALLOWED_USER_ID`.
2. `runner.py` serializes one Codex invocation at a time. Each job
   runs in a detached git worktree and streams `codex --json` events
   into `logs/<job-id>/`.
3. The final Codex answer is redacted by `redaction.py` and sent
   back to Telegram.
4. `codex-telegram-maintain.timer` runs `auto_maintain.py` once an
   hour to clean up old jobs and run read-only diagnostics.

For the endpoint table, env vars, smoke gate, operator cheat sheet,
and what is intentionally out of scope, see [`project.md`](project.md).

## File Structure

```text
telegram_codex_runner/
  bot.py                         # Telegram command handlers
  config.py                      # .env loading and validation
  runner.py                      # job queue, git worktrees, codex subprocess
  redaction.py                   # Telegram output redaction and truncation
  requirements.txt
  .env.example
  systemd/codex-telegram-bot.service
  systemd/codex-telegram-maintain.service
  systemd/codex-telegram-maintain.timer
  README.md
```

## Commands

- Plain text messages run Codex with `--sandbox read-only`, the same as `/run <prompt>`.
- `/run <prompt>` runs Codex with `--sandbox read-only`.
- `/fix <prompt>` runs Codex with `--sandbox workspace-write`.
- `/status` shows the current or last job.
- `/jobs [limit]` shows recent jobs.
- `/last` shows the latest final result.
- `/diff` shows `git status --short`, `git diff --stat`, and a truncated diff preview from the last job worktree.
- `/apply` applies the latest job worktree changes back to the main repo only when the main repo is clean.
- `/discard` removes the latest job worktree.
- `/clean [keep]` removes old job logs and worktrees while keeping recent jobs.
- `/maintain [keep]` runs the self-maintenance harness: service checks, workspace checks, MiniMax checks, disk/runtime checks, latest-job checks, and conservative cleanup when logs or worktrees exceed the threshold.
- `/diag [since]` returns a compact all-in-one diagnostics report combining doctor, metrics, job audit, rate-limit report, security audit, and latest log summary.
- `/health [full] [json] [nosecurity]` returns a fast health snapshot summary. Add `full` to run offline harnesses and security checks; add `json` for machine-readable JSON.
- `/audit [stale-minutes]` audits stored job logs and worktrees for stale running jobs, orphan worktrees, missing worktrees, and failed-job samples.
- `/log [job-id|prefix|latest]` safely summarizes the latest attempt log without dumping raw JSONL.
- `/meta [job-id|prefix|latest]` shows structured `job.json` metadata for a new-format job.
- `/metrics [limit]` summarizes recent job states, success rate, rate-limit hits, token usage totals, and recent job previews.
- `/security [since]` audits `.env` permissions, repository token literals, recent service logs, and systemd hardening without printing secrets.
- `/ratelimit [limit]` reports recent `429`/rate-limit events found in stored Codex job logs.
- `/editcheck` builds a temporary git repo, asks Codex to edit a real file, verifies the diff and final file content, then removes the temporary repo.
- `/cancel` terminates the running Codex process when possible.

Only one job runs at a time for the configured repository. Telegram replies are intentionally quiet: the bot sends a short start acknowledgement, useful retry/failure notices, and the final Codex answer. Raw Codex JSONL events stay on disk in the job logs. If Codex exits because the provider returns `429 Too Many Requests`, the runner retries the whole Codex attempt using `CODEX_RETRY_429_DELAYS_SECONDS`.

## Setup On Ubuntu

1. Install Codex CLI and authenticate it for the `ubuntu` user.

   ```bash
   codex doctor
   codex exec --json --sandbox read-only "Say ready"
   ```

2. Copy this directory to the VPS.

   ```bash
   sudo mkdir -p /opt/codex-telegram-runner
   sudo chown -R ubuntu:ubuntu /opt/codex-telegram-runner
   rsync -av telegram_codex_runner/ <ssh-user>@<vps-host>:/opt/codex-telegram-runner/
   ```

3. Create a virtual environment.

   ```bash
   cd /opt/codex-telegram-runner
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. Configure `.env`.

   ```bash
   cp .env.example .env
   nano .env
   chmod 600 .env
   ```

   Required values:

   ```dotenv
   TELEGRAM_BOT_TOKEN=123456789:replace_me
   TELEGRAM_ALLOWED_USER_ID=123456789
   CODEX_WORKSPACE_ROOT=/srv/my-repo
   CODEX_BIN=/usr/local/bin/codex
   OPENAI_API_KEY=sk-replace_me
   MINIMAX_API_KEY=sk-replace_me
   ```

   `CODEX_WORKSPACE_ROOT` must be the top-level directory of a git repository.
   Use either `OPENAI_API_KEY` for OpenAI auth or `MINIMAX_API_KEY` with a MiniMax provider configured in `~/.codex/config.toml`.

   Example MiniMax provider for Codex CLI:

   ```toml
   model = "MiniMax-M3"
   model_provider = "minimax"
   model_reasoning_effort = "low"

   [model_providers.minimax]
   name = "MiniMax"
   base_url = "https://api.minimaxi.com/v1"
   env_key = "MINIMAX_API_KEY"
   wire_api = "responses"
   ```

   If `https://api.minimax.io/v1` returns `401 invalid api key`, use the `api.minimaxi.com` endpoint that matches the Anthropic-compatible token-plan key. Temporary `429 Too Many Requests` errors mean the provider accepted the request path but is rate limiting or overloaded.

   Optional retry tuning:

   ```dotenv
   CODEX_RETRY_429_DELAYS_SECONDS=300,900,1800
   ```

   The safer VPS path is to run the interactive helper, which does not echo secrets:

   ```bash
   cd /opt/codex-telegram-runner
   .venv/bin/python scripts/configure_env.py
   scripts/healthcheck.sh
   ```

5. Install the systemd service.

   ```bash
   sudo cp systemd/codex-telegram-bot.service /etc/systemd/system/codex-telegram-bot.service
   sudo cp systemd/codex-telegram-maintain.service /etc/systemd/system/codex-telegram-maintain.service
   sudo cp systemd/codex-telegram-maintain.timer /etc/systemd/system/codex-telegram-maintain.timer
   sudo systemctl daemon-reload
   sudo systemctl enable --now codex-telegram-bot
   sudo systemctl enable --now codex-telegram-maintain.timer
   sudo journalctl -u codex-telegram-bot -f
   ```

## Operator CLI

For maintenance and testing, you can bypass the Telegram client UI while still sending results back through the bot API:

```bash
cd /opt/codex-telegram-runner
.venv/bin/python scripts/send_message.py "Bot API direct send works"
.venv/bin/python scripts/submit_job.py "Reply exactly OK"
.venv/bin/python scripts/submit_job.py --mode fix "Make a small repo change"
.venv/bin/python scripts/lifecycle.py jobs
.venv/bin/python scripts/lifecycle.py last
.venv/bin/python scripts/lifecycle.py diff
.venv/bin/python scripts/lifecycle.py apply
.venv/bin/python scripts/lifecycle.py discard
.venv/bin/python scripts/lifecycle.py clean --keep 20
.venv/bin/python scripts/auto_maintain.py --notify
systemctl list-timers codex-telegram-maintain.timer
```

`send_message.py` only sends a Telegram notification. `submit_job.py` runs the same `CodexRunner` backend used by Telegram messages, then sends progress and the final result to the configured `TELEGRAM_ALLOWED_USER_ID`.

## Harnesses

Use these after backend changes:

```bash
cd /opt/codex-telegram-runner
.venv/bin/python scripts/replay.py
.venv/bin/python scripts/doctor.py --send-test
.venv/bin/python scripts/doctor.py --json
.venv/bin/python scripts/auto_maintain.py
.venv/bin/python scripts/backfill_metadata.py
.venv/bin/python scripts/command_harness.py
.venv/bin/python scripts/fault_harness.py
.venv/bin/python scripts/offline_harnesses.py
.venv/bin/python scripts/health_snapshot.py --fast
.venv/bin/python scripts/health_snapshot.py --fast --write
.venv/bin/python scripts/health_snapshot.py
.venv/bin/python scripts/diagnostics.py --since "1 hour ago"
.venv/bin/python scripts/job_audit.py --stale-minutes 90
.venv/bin/python scripts/log_summary.py latest
.venv/bin/python scripts/metadata_report.py latest
.venv/bin/python scripts/metrics_report.py --limit 20
.venv/bin/python scripts/security_audit.py --since "1 hour ago"
.venv/bin/python scripts/rate_limit_report.py
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/edit_harness.py
```

- `replay.py` is offline and cheap. It replays fixture Codex JSONL and verifies Telegram output filtering does not leak `thread.started`, worktree paths, queued messages, or other noisy internals.
- `doctor.py` does not run generation. It checks systemd, workspace, MiniMax `/models`, disk space, runtime directories, latest job logs, and optional Telegram `sendMessage`.
- `auto_maintain.py` wraps the doctor checks, cheap offline harnesses, metadata backfill, persisted health snapshots, and conservative cleanup when logs/worktrees cross the configured threshold. Use `--notify` to send the summary to Telegram.
- `backfill_metadata.py` creates missing `job.json` sidecars for legacy logs without overwriting newer metadata unless `--force` is passed.
- `command_harness.py` is an offline Telegram command matrix. It fakes authorized/unauthorized updates, patches heavy backends, and verifies command handlers, argument clamping, and reply wiring without calling Telegram or Codex.
- `fault_harness.py` is an offline runner state-machine gate. It injects success, non-429 failure, 429 retry success, exhausted 429 retry, and cancellation during retry wait without calling Codex or the provider.
- `offline_harnesses.py` runs the cheap offline gates (`replay.py`, `command_harness.py`, and `fault_harness.py`) as subprocesses and returns compact health lines suitable for `/diag` and scheduled maintenance.
- `health_snapshot.py` emits a machine-readable JSON snapshot with doctor checks, job audit, recent metrics, latest job metadata, recent rate-limit events, and triage hints. Use `--fast` to skip offline harnesses and security checks; omit it for a full gate. Add `--write` to persist it under `CODEX_TASK_ROOT/health/`.
- `diagnostics.py` composes the main read-only harnesses into one bounded report for first-response debugging, including triage hints when a check fails.
- `job_audit.py` reports stale `running` logs, orphan worktrees, logs without worktrees, and failed-job samples. It is read-only; use `/clean` or `/discard` for removal.
- `log_summary.py` prints a redacted, bounded summary of the latest or selected job log: job id, attempt file, update time, final text, and recent interesting events.
- `metadata_report.py` prints the structured `job.json` sidecar for new jobs, including state, attempts, return code, duration, rate-limit flag, usage counters, key paths, and redacted event/error/summary fields.
- `metrics_report.py` aggregates recent jobs into a compact trend view: state counts, success rate, rate-limit hits, usage totals, and recent job previews. It prefers `job.json` when available and falls back to JSONL for legacy jobs.
- `security_audit.py` checks secret hygiene and hardening without printing sensitive values. Use a narrower `--since` window after rotating credentials if old journal entries still contain a revoked token.
- `rate_limit_report.py` scans stored Codex JSONL logs for `429`, `too many requests`, `rate limit`, and `high demand` markers so cooldown events are visible without opening raw logs.
- `smoke.py` is the full end-to-end gate. It checks systemd, MiniMax `/models`, Telegram `sendMessage`, runs a tiny Codex job, verifies `turn.completed`, and confirms the final answer exactly matches the expected smoke token.
- `edit_harness.py` is the real-edit gate. It creates a temporary git repository, runs the same workspace-write runner path as `/fix`, verifies Codex edited `status.txt`, checks the git diff and `turn.completed`, then deletes the temporary repository.

## Runtime Data

By default, task data is stored next to the configured repo:

```text
<parent-of-repo>/codex-telegram-runner/
  logs/<job-id>/attempt-1.jsonl
  logs/<job-id>/attempt-1-final.txt
  logs/<job-id>/job.json
  logs/<job-id>/attempt-2.jsonl
  logs/<job-id>/attempt-2-final.txt
  worktrees/<job-id>/
```

Set `CODEX_TASK_ROOT` in `.env` to move this elsewhere.

## Safety Model

- Telegram access is denied unless `effective_user.id` exactly matches `TELEGRAM_ALLOWED_USER_ID`.
- Telegram prompts are passed only to Codex stdin. They are not executed as shell commands.
- `/run` uses Codex `read-only` sandbox by default.
- `/fix` uses Codex `workspace-write` sandbox by default.
- `danger-full-access` is never used by this service.
- Each job uses a detached git worktree created from `HEAD`.
- The service keeps raw Codex JSONL logs on disk, but Telegram output is truncated and redacted for common secret patterns.
- The bot process suppresses verbose HTTP client request logs and redacts common token patterns before log records are emitted.
- The systemd units set `PYTHONDONTWRITEBYTECODE=1` so runtime imports do not leave `__pycache__` files in the deployed code directory.
- The bot does not commit, push, or merge changes.
- The child process receives a reduced environment, keeping only basic OS variables plus `CODEX_*`, `OPENAI_*`, and `AZURE_OPENAI_*` values that Codex may need for auth.

This is still remote code-running infrastructure: keep the bot token private, use a dedicated Telegram bot, keep the VPS user low privilege, and review `/diff` before manually applying or copying changes.

## Later: Approval Buttons

A future version can add inline Telegram buttons for higher-risk steps:

- `Approve write` before starting `/fix`.
- `Show full diff` with a file attachment instead of a long message.
- `Apply to main worktree` after a `/fix` job.
- `Commit` and `Push` as separate explicit actions.

Keep those actions separate from `/run` and `/fix`; the current MVP intentionally stops at producing a worktree and a diff.
