# Conveyor Web Console

The Web Console is a browser control surface over Conveyor's existing queue,
Codex runner, worktrees, apply policy, node registry, computer kill switch, and
Secure Human Takeover coordinator. It is not a second execution engine and it
does not require a browser or Node.js process on the VPS.

## Runtime shape

```text
browser ── bearer-authenticated REST + SSE ── web_console_takeover.py
                                                   │
Telegram ─┐                                        │
Feishu ───┼── shared SQLite FIFO ── CodexRunner ── worktrees
Web ──────┘             │
                       agent_events (ordered replay)

browser ── takeover API ── HumanTakeoverStore
                               │
                               v
                     conveyor-handoff.service
                               │
                               v
                     loopback noVNC / x11vnc
                               │
                        optional Tailscale Serve
```

The frontend in `web/` is built ahead of deployment. The Web entrypoint serves
`web/dist`, the API, and one low-frequency SSE replay stream per selected job.
Production does not run Vite or any other Node server.

The handoff transport is deliberately owned by a separate systemd sidecar. The
Web server cannot spawn arbitrary GUI transport commands; it only creates or
updates secret-free takeover coordination state.

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

The standard installer installs both `conveyor-web.service` and
`conveyor-handoff.service`. For a manual source deployment:

```bash
sudo cp /opt/conveyor/systemd/conveyor-handoff.service /etc/systemd/system/
sudo cp /opt/conveyor/systemd/conveyor-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conveyor-handoff.service conveyor-web.service
sudo systemctl status conveyor-handoff.service conveyor-web.service
```

The Web service refuses to start when the feature flag is off or the token is
under 32 characters. Keep the default loopback bind.

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

Secure Human Takeover has its own private route requirements. See
[`human_takeover.md`](human_takeover.md); public VNC/noVNC ports and Tailscale
Funnel are outside the supported model.

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
- `POST /api/chat` (SSE streaming conversation without Codex)
- `GET /api/chat/history?session_id=...` (retrieves session chat transcript)
- `POST /api/tasks`, `POST /api/jobs/{id}/cancel`
- `POST /api/jobs/{id}/apply`, `POST /api/jobs/{id}/discard`
- `GET /api/approvals`
- `POST /api/approvals/{id}/approve`, `/reject`
- `GET /api/nodes`, `GET /api/nodes/{id}`
- `GET /api/computer/status`, `POST /api/computer/stop`
- `POST /api/computer/screenshot` for an explicit, one-shot host screenshot request
- `GET /api/artifacts/{id}` for allow-listed screenshot thumbnails
- `GET /api/takeover/status`
- `POST /api/takeover/start`
- `POST /api/takeover/activate`
- `POST /api/takeover/complete`
- `POST /api/takeover/cancel`

### Chat tier and SSE streaming (`/api/chat`)

The web console exposes Conveyor's direct chat tier (`handlers.chat.ask_chat`) via `POST /api/chat`. It answers conversation in seconds without queuing a Codex agent job.

- **Request**: `POST /api/chat` with JSON body:
  ```json
  {
    "message": "Check system status",
    "session_id": "web:web-console:web-12345"
  }
  ```
  `session_id` is optional; when omitted, a fresh web session is generated. If the chat tier is disabled (`chat_enabled(settings)` is false), the endpoint returns `409 Conflict`.
- **Response**: Server-Sent Events (`text/event-stream`, `Cache-Control: no-store`, `X-Accel-Buffering: no`). Event types:
  - `session`: `{"session_id": "<id>"}` emitted first.
  - `delta`: `{"text": "<partial>"}` live streaming tokens.
  - `status`: `{"text": "<note>"}` progress notes (e.g. search status or delegation hints).
  - `approval`: `{"id": "<token>", "tool_name": "...", "arg": "...", "summary": "...", "text": "...", "expires_in_seconds": 300}` when a write/dangerous tool requires confirmation.
  - `message`: `{"text": "<final answer>"}` complete assistant response.
  - `error`: `{"error": "<redacted error>"}` on execution failure or timeout.
  - `done`: `{"outcome": "answered" | "escalate" | "unavailable"}` emitted last.

If the model escalates or the tier is unavailable, a message advises the operator to submit the request as a task via the composer, and `done` finishes with the respective outcome.

### Chat history (`/api/chat/history`)

- `GET /api/chat/history?session_id=<id>` retrieves the transcript for a session from `TranscriptStore`.
- Response:
  ```json
  {
    "session_id": "web:web-console:web-12345",
    "messages": [
      { "role": "user", "text": "...", "created_at": "..." },
      { "role": "assistant", "text": "...", "created_at": "..." }
    ]
  }
  ```
  Returns `404 Not Found` if the session does not exist.

### Tool approvals (`/api/approvals`)

- `GET /api/approvals`: Returns pending decisions including both Codex job diff approvals (`"kind": "job"`) and live chat tool confirmations (`"kind": "tool"`). Tool approvals have the shape:
  ```json
  {
    "id": "<token>",
    "kind": "tool",
    "tool_name": "service_restart",
    "arg": "conveyor",
    "summary": "重启 Conveyor systemd 服务",
    "session_id": "web:web-console:web-12345",
    "status": "pending",
    "expires_at": 1790764680.0
  }
  ```
- `POST /api/approvals/{id}/approve` and `POST /api/approvals/{id}/reject`:
  - For pending web tool actions, executes or cancels the tool, appends the outcome to the session transcript as an assistant message, and returns `{"id": "<token>", "kind": "tool", "status": "accepted"|"rejected", "result": "..."}`.
  - Subsequent approval attempts return `404 Not Found`.
  - Non-web pending actions (from Telegram or Feishu) cannot be decided from the web console and return `404 Not Found`.
  - For job approvals, falls through to the existing apply/discard logic unchanged.

### Unified Approval Inbox (`/api/approval-inbox`)

For advanced review with editable drafts across Web Chat, routines, webhooks, and job worktrees, see [Unified Approval Inbox](approval_inbox.md). Enabled via `CONVEYOR_APPROVAL_INBOX_ENABLED=true`.

### Human takeover semantics

Secure Human Takeover is off by default (`CONVEYOR_TAKEOVER_ENABLED=false`).
To enable takeover endpoints and the transport sidecar, set
`CONVEYOR_TAKEOVER_ENABLED=true` in `.env` (honored by both the web entrypoint
`web_console_takeover.py` and the sidecar `handoff_sidecar.py`). When disabled,
`/api/takeover/status` returns `enabled: false` and mutation endpoints respond
with `403 Forbidden`.

`POST /api/takeover/start` establishes exclusive GUI ownership before a remote
desktop is opened. It cancels pending pre-handoff computer/screenshot work and
waits for already-claimed work to finish. The systemd handoff sidecar then
starts the fixed noVNC transport.

`complete` and `cancel` are intentionally asynchronous close requests. They do
**not** immediately release the lease. The Web Workbench continues showing
Privacy Mode while `conveyor-handoff.service` removes remote access and verifies
cleanup; only then is the takeover transitioned to its terminal state and Agent
GUI automation allowed to continue.

Takeover API responses contain coordination metadata and a safe handoff URL
only. They never contain the temporary VNC credential, typed text, payment data,
passwords, clipboard contents, screenshots, or password-manager material.

Host-screen capture remains read-only and opt-in. `POST /api/computer/screenshot`
is available only when `CONVEYOR_DESKTOP_UPLOAD_ENABLED=true`, the Mac desktop
agent is online, and its screenshot helper is configured. It requests one
capture; Conveyor transfers only the configured, size-limited thumbnail to the
VPS for the authenticated Web Console. The original stays on the Mac. The
browser does not start a continuous stream and gains no mouse or keyboard
control from this feature. Thumbnail upload remains disabled by default.

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

At idle the Web feature adds one Python Web process and the lightweight sleeping
handoff coordinator process. There is no Node process, Chromium, broker, or
metrics worker. The handoff sidecar does not start x11vnc/websockify unless an
open takeover lease exists, and it throttles failed transport retries and
unchanged status writes.

## General Routines and Inbox

The Web Console serves as the execution host and inbox for [General Routines](routines.md).
Because pending tool approvals live in-memory in the process that spawned them, routine
execution runs on the server loop of `web_console.py`. Scheduled runs and tool approvals
appear directly in the Web Console Inbox tab.

## Current limitations

- The first version is single-operator bearer authentication, not multi-user RBAC.
- A running Codex process can only be signalled by the Conveyor process that owns
  it; cross-process queued cancellation is supported, but cross-process running
  cancellation returns a conflict instead of lying about success.
- Events created before this feature have queue/job metadata but no reconstructed
  historical transcript.
- Web takeover still requires the same validated VPS X11/XAUTHORITY and noVNC
  prerequisites as the CLI flow. The Web API does not provision a graphical
  session or weaken VNC authentication.
