# Conveyor Web Console

The Web Console is a browser control surface over Conveyor's existing queue,
Codex runner, worktrees, apply policy, node registry, and computer kill switch.
It is not a second execution engine and it does not require a browser or Node.js
process on the VPS.

## Runtime shape

```text
browser ── bearer-authenticated REST + SSE ── web_console.py
                                                   │
Telegram ─┐                                        │
Feishu ───┼── shared SQLite FIFO ── CodexRunner ── worktrees
Web ──────┘             │
                       agent_events (ordered replay)
```

The frontend in `web/` is built ahead of deployment. `web_console.py` serves
`web/dist`, the API, and one low-frequency SSE replay stream per selected job.
Production does not run Vite or any other Node server.

## Secure setup

Generate a token locally or on the VPS without putting it in shell history:

```bash
openssl rand -hex 32
```

Add these values to `/opt/conveyor/.env` (mode `0600`):

```dotenv
CONVEYOR_WEB_ENABLED=true
CONVEYOR_WEB_HOST=127.0.0.1
CONVEYOR_WEB_PORT=8787
CONVEYOR_WEB_TOKEN=<generated 64-hex-character value>
CONVEYOR_EVENT_RETENTION_PER_JOB=2000
```

Install/start the unit:

```bash
sudo cp /opt/conveyor/systemd/conveyor-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conveyor-web.service
sudo systemctl status conveyor-web.service
```

The service refuses to start when the feature flag is off or the token is under
32 characters. Keep the default loopback bind.

### SSH tunnel

```bash
ssh -N -L 8787:127.0.0.1:8787 vps-oracle
```

Open `http://127.0.0.1:8787` and enter the bearer token. The token is retained
only in browser `sessionStorage`, not a persistent cookie or server log.

### Tailscale or reverse proxy

Loopback plus an SSH tunnel is the smallest secure default. A Tailscale Serve
or TLS reverse proxy may forward to `127.0.0.1:8787`; keep TLS and an additional
proxy authentication layer when the proxy is reachable beyond a private tailnet.
Do not open the port directly in UFW.

## Build and test

```bash
make web-test
make web-build
```

`make web-build` requires Node.js only on the build machine and writes static
assets to `web/dist`. Deploy those assets with the Python source. ARM64 runtime
has no new compiled Python dependency.

## API

`GET /api/health` is intentionally minimal and unauthenticated. Every other API
requires `Authorization: Bearer <token>`.

- `GET /api/system/status`
- `GET /api/sessions`, `GET /api/sessions/{id}`
- `GET /api/jobs`, `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/events`, `GET /api/jobs/{id}/diff`
- `GET /api/events/stream?job_id=...&after=...`
- `POST /api/tasks`, `POST /api/jobs/{id}/cancel`
- `POST /api/jobs/{id}/apply`, `POST /api/jobs/{id}/discard`
- `GET /api/approvals`
- `POST /api/approvals/{id}/approve`, `/reject`
- `GET /api/nodes`, `GET /api/nodes/{id}`
- `GET /api/computer/status`, `POST /api/computer/stop`
- `GET /api/artifacts/{id}` for allow-listed screenshot thumbnails

Apply and discard endpoints create a five-minute, job-scoped approval. They do
not mutate the worktree until the matching authenticated approval endpoint is
called. Browser disconnects never approve, apply, discard, cancel, or arm CUA.

## Event protocol and retention

Each persisted event contains `schema_version`, collision-safe `event_id`, a
per-job `sequence`, UTC ISO-8601 `timestamp`, `kind`, `job_id`, optional session/
tool/correlation identifiers, and an evolvable JSON payload. The browser replays
after its last sequence and deduplicates by event ID.

Payloads pass through Conveyor redaction and are bounded. Reasoning events are
not stored. Screenshots remain files and events/API responses reference an
artifact identifier. The default retains the latest 2,000 events per job.

## Resource profile

At idle the feature adds one Python process plus one sleeping HTTP thread. There
is no Node process, Chromium, broker, or metrics worker. System status refreshes
every 15 seconds in an open browser; SSE checks SQLite every 750 ms only while a
job view is open. No browser connection means no event polling thread.

## Current limitations

- The first version is single-operator bearer authentication, not multi-user RBAC.
- A running Codex process can only be signalled by the Conveyor process that owns
  it; cross-process queued cancellation is supported, but cross-process running
  cancellation returns a conflict instead of lying about success.
- Events created before this feature have queue/job metadata but no reconstructed
  historical transcript.
