# Conveyor overnight preview iteration

The three-role coordinator is installed in the isolated preview environment. Its checks cover configured acceptance scenarios; successful completion does not establish that a website has no bugs. The delivery target is the dedicated `preview` branch and isolated loopback service on port 18790; production `main` and `/opt/conveyor` are outside the authorized delivery scope.

## Operator entry point

The server coordinator lives under `/srv/conveyor-preview/coordinator`. Its `controller` checkout contains the installed, reviewed coordinator revision, `repository` is a separate checkout of preview, `tools/gh` is a checksum-verified modern GitHub CLI, and `config.json` selects the repository and fixed verification commands. Do not place credentials in config.json. GitHub credentials are read by the coordinator; Pi and verification execute inside bubblewrap without the coordinator home or production filesystem. A private optional `provider.env` supplies only the model provider key.

The preview Chat view accepts these deterministic commands even when model chat is disabled:

```text
启动夜间任务：检查工作台并修复已配置验收中的问题
/nightly_start Check the preview Workbench
/nightly_status RUN_ID
/nightly_cancel RUN_ID
```

Start returns a durable run ID, deadline and budget. Defaults are one hour and twelve role tasks, with at most three repair attempts per issue. Only one run may be active. Cancel revokes claims and stops that run's process group. Status includes issue, PR and deployed revision evidence as it becomes available. Production chat does not enable this control path.

With `"objective_planning": true` in coordinator `config.json`, discovery reads the operator goal and source to generate one to three Python acceptance checks. The plan is persisted with its objective, initial revision and SHA-256 under the current discovery lease before any finding can create an issue. Later discoveries, independent candidate verification and post-deployment checks reuse that exact plan. Cancellation or a reclaimed lease rejects late plans; neither Pi repair nor a resumed task may weaken it. The status report lists the frozen scenarios and digest. Repeating the exact goal with the same configuration reuses its existing contracts, keeping reproduction text stable for Issue deduplication; a different goal or configuration generates a new plan.

Planning reads source through a read-only mount and writes only a private plan artifact; generated bundles are hidden. Generated checks execute without provider credentials, host network access or source writes. Planning is bounded to 300 seconds, each generated check to 60 seconds, with the outer run deadline still enforced. Operator-owned HTTP scenarios and `source_probes` remain mandatory alongside the generated checks. Config changes during a run are rejected.

When objective planning is disabled, the objective only records intent and discovery uses the fixed configured scenarios. Automatic planning covers source contracts and offline Python behavior, not general autonomous browser exploration. Review a goal's reported scenarios when judging coverage; two clean passes never prove an entire site bug-free.

For CLI operation, record objective, deployed revision, acceptance checks, deadline and task budget first:

```sh
cd /srv/conveyor-preview/coordinator/controller
/srv/conveyor-preview/venv/bin/python overnight_cli.py --db /srv/conveyor-preview/coordinator/runs.sqlite3 create --objective 'Check Conveyor preview health and session browsing' --revision FULL_DEPLOYED_SHA --hours 1 --max-tasks 12 --config /srv/conveyor-preview/coordinator/config.json
```

Start the returned run ID using `sudo systemctl start conveyor-nightly@RUN_ID.service`. Use the CLI `status RUN_ID` to inspect durable outcomes and `cancel RUN_ID` to revoke claims; stop the service as well for immediate process termination. `journalctl -u conveyor-nightly@RUN_ID.service` shows process errors without printing provider output. Never put secrets in objectives or issue evidence.

The coordinator runs three roles, deduplicates issue fingerprints, repairs only allowed Workbench/test source, and independently verifies a pinned candidate. Candidate CI must pass before preview merge. Deployment selects that exact candidate's merge commit and restores the previous release if health fails. Two clean discovery passes finish a run. Time/task exhaustion and failed attempts become explicit terminal outcomes; no indefinite iteration is intended.

## Current evidence and remaining limits

- Real preview issue #157 was repaired by Pi in PR #158; immutable reproduction and chat regressions passed, all three CI jobs passed, preview merge/deployment succeeded and the issue closed only after live reproduction passed. This rehearsal required operator intervention for Git credentials and deployment compatibility; subsequent commits address those failures.
- A separate unprivileged run completed two clean passes with automatic GitHub preflight and durable completion. Same-version preview deployment recovery preserved rollback evidence.
- Discovery currently covers page loading, authenticated health and the sessions endpoint. The immutable streaming reproduction is an additional operator-supplied acceptance probe, not a general autonomous browser explorer. The Chinese start command and explicit status/cancel chat commands are connected. Optional objective planning adds frozen source/behavior acceptance checks; broad autonomous browser exploration remains outside current coverage.
- Cancellation fences new adapter commands and release switching. A GitHub request already accepted before cancellation cannot be undone; resume must reconcile its result. Restart leases recover unfinished tasks, but cross-host coordinators are not supported.
- Resource limits currently cover wall-clock time, task count, three repair attempts, command deadlines and output bytes. A monetary/token-spend cap is not implemented. Do not advertise such a cap.

The clean baseline alone is not full-loop evidence. Run `7ed0f76c265c4f46918b39f86ae0783e` automatically discovered #164, created Pi PR #167, passed CI and deployed, but safely blocked during post-deployment recovery. PR #168 removes the systemd hard dependency that stopped the worker during preview restart, and journals release metadata before switching. Its interruption regression and all CI checks pass. The original failed run remains evidence; operator recovery and closure of #164 are not counted as an unattended success.

Run `fafe3920fdf8447a8506fc07c11fa906` completed the real full unattended loop: rediscovered/deduplicated #169, Pi authored PR #171, independent checks and all CI passed, the worker merged and built/deployed preview `c775ca7f662fcdc4a076cc55ee5844e6cbfc5fe8`, live checks passed before issue closure, and two further clean discovery passes produced durable `completed`. No manual repair, merge, deployment or issue closure was performed during this run. It used the configured acceptance probes.

Run `ea5fbd45136546128af1a91ce9f47f8b` separately completed the goal-only acceptance: one Chinese high-level objective, no new operator-written probe, three Pi-generated frozen contracts, Issue #173, Pi PR #175, seven independent checks and all CI, automatic preview build/deployment `e9d61e0a3e0839af4809b979c802e5025b0ac808`, verified issue closure and two clean passes. Real Chromium confirmed that the displayed Chinese-goal example is accepted by the actual chat router. A separate real Web cancellation during Pi planning stopped all unit children and rejected any late frozen plan (`2d7d9f2e8321499aa5035f0f89977273`).

## Service and release recovery

Install `systemd/conveyor-nightly@.service` and `systemd/conveyor-nightly-control.service`, then reload systemd. Both use `Wants=conveyor-preview.service`: a preview restart must not stop the coordinator performing that restart. Keep the control bridge enabled; it resumes nonterminal runs on boot. The per-run worker reclaims expired leases and reconciles a prior PR or merged candidate before publishing again.

Releases are built from tracked source with the pinned npm lockfile inside a sandbox. `current` switches only after build succeeds. A durable `deployment-pending.json` records the new and previous revisions before switching; recovery reactivates the target service and validates health before finalizing `deployment.json`, and retains the previous release for rollback. Do not manually rewrite these files to make a failed run appear successful.

For local preview access, forward its isolated loopback listener:

```sh
ssh -N -L 18791:127.0.0.1:18790 vps-oracle
```

Open `http://127.0.0.1:18791` and use the preview's own bearer token through the login screen. Read it privately from preview runtime configuration; do not use a production token, put tokens in URLs, or publish them in logs/issues.
