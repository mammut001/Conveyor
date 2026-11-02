# Conveyor overnight preview iteration

This feature is being validated in draft PR #156. It must not be represented as an unrestricted or bug-free website maintainer. The delivery target is the dedicated `preview` branch and isolated loopback service on port 18790; production `main` and `/opt/conveyor` are outside the authorized delivery scope.

## Operator entry point

The server coordinator lives under `/srv/conveyor-preview/coordinator`. Its `controller` checkout contains this feature branch, `repository` is a separate checkout of preview, `tools/gh` is a checksum-verified modern GitHub CLI, and `config.json` selects the repository and fixed verification commands. Do not place credentials in config.json. GitHub credentials are read by the coordinator; Pi and verification execute inside bubblewrap without the coordinator home or production filesystem. A private optional `provider.env` supplies only the model provider key.

Create a run after recording its objective, deployed revision, acceptance checks, deadline and task budget:

```sh
cd /srv/conveyor-preview/coordinator/controller
/srv/conveyor-preview/venv/bin/python overnight_cli.py --db /srv/conveyor-preview/coordinator/runs.sqlite3 create --objective 'Check Conveyor preview health and session browsing' --revision FULL_DEPLOYED_SHA --hours 1 --max-tasks 12
```

Start the returned run ID using `sudo systemctl start conveyor-nightly@RUN_ID.service`. Use the CLI `status RUN_ID` to inspect durable outcomes and `cancel RUN_ID` to revoke claims; stop the service as well for immediate process termination. `journalctl -u conveyor-nightly@RUN_ID.service` shows process errors without printing provider output. Never put secrets in objectives or issue evidence.

The coordinator runs three roles, deduplicates issue fingerprints, repairs only allowed Workbench/test source, and independently verifies a pinned candidate. Candidate CI must pass before preview merge. Deployment selects that exact candidate's merge commit and restores the previous release if health fails. Two clean discovery passes finish a run. Time/task exhaustion and failed attempts become explicit terminal outcomes; no indefinite iteration is intended.

## Current evidence and remaining limits

- Real preview issue #157 was repaired by Pi in PR #158; immutable reproduction and chat regressions passed, all three CI jobs passed, preview merge/deployment succeeded and the issue closed only after live reproduction passed. This rehearsal required operator intervention for Git credentials and deployment compatibility; subsequent commits address those failures.
- A separate unprivileged run completed two clean passes with automatic GitHub preflight and durable completion. Same-version preview deployment recovery preserved rollback evidence.
- Discovery currently covers page loading, authenticated health and the sessions endpoint. The immutable streaming reproduction is an additional operator-supplied acceptance probe, not a general autonomous browser explorer. Broad UI coverage and natural-language task submission are not yet connected.
- Cancellation fences new adapter commands and release switching. A GitHub request already accepted before cancellation cannot be undone; resume must reconcile its result. Restart leases recover unfinished tasks, but cross-host coordinators are not supported.
- Resource limits currently cover wall-clock time, task count, three repair attempts, command deadlines and output bytes. A monetary/token-spend cap is not implemented. Do not advertise such a cap.

The clean baseline and repair rehearsal do not prove every restart window or a completely unattended repair/deploy run. Keep the implementation draft until those acceptance gates are satisfied.
