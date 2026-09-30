# Secure Human Takeover

Conveyor should not try to automate every GUI step.

Some moments are deliberately human-only: passwords, payment details, CAPTCHA,
identity verification, consent dialogs, and any other step where the operator
must personally inspect or enter sensitive information.

The product model is:

```text
Agent working
    |
    | sensitive / human-only step
    v
waiting_for_human
    |
    | operator opens temporary remote GUI
    v
human_active
    |
    | operator completes the sensitive step
    v
transport cleanup verified
    |
    v
completed
    |
    | Agent observes the new state
    v
Agent continues
```

## Why noVNC

For a VPS that already has a graphical X11 session, a small noVNC stack is a
reasonable transport because it lets the operator enter through a normal Web
browser. The recommended topology is:

```text
Operator browser
      |
      | SSH tunnel / VPN / authenticated reverse proxy
      v
127.0.0.1:6080  noVNC/websockify
      |
      v
127.0.0.1:5901  x11vnc
      |
      v
existing VPS graphical session
```

**Neither port is public.** `scripts/novnc_handoff.sh` binds both listeners to
loopback and does not touch firewall rules. Public `0.0.0.0:5901` or
`0.0.0.0:6080` is outside the supported security model.

Other transports (RDP, Guacamole, a VPN-native remote desktop, etc.) can be
used later. Conveyor's core contract is transport-independent.

## Security contract

### Exclusive GUI ownership

A human takeover is an exclusive GUI lease. While a takeover is open:

- Conveyor's computer-use loop pauses before every new action.
- No automated `observe`, click, type, hotkey, scroll, or follow-up screenshot
  is created by the loop.
- Operator stop/cancel remains authoritative.
- Takeover time does not consume the task's automation wall-clock budget.
- A second concurrent takeover cannot be opened.

This avoids an agent clicking while the operator is typing a password or card
number.

### Privacy mode

During a human takeover, Conveyor treats the desktop as privacy-sensitive:

- no screenshot recording for the Agent;
- no OCR/vision analysis;
- no typed-text logging;
- no clipboard capture;
- no credential or payment value enters Conveyor state;
- the persistent takeover record contains coordination metadata only.

The `human_takeover.sqlite3` database stores only the session id, state, reason,
optional task id/operator label, timestamps, TTL, and close reason.

### Payment/password boundary

The existing computer-use blocked-keyword policy remains in force. Human
takeover is **not** a way to let the Agent type blocked values. It is the
opposite: automation yields the GUI to the operator.

For example:

```text
Agent: checkout page is ready; payment requires you
        |
        v
Conveyor opens takeover lease
        |
        v
Operator enters card number / CVV directly in remote desktop
        |
        v
Operator requests handoff completion
        |
        v
sidecar closes remote access and verifies cleanup
        |
        v
Agent receives only the post-handoff page state
```

Conveyor must never receive the card number, CVV, password, or password-manager
contents as task input, trajectory text, screenshot OCR, or audit output.

## Takeover state machine

States:

- `waiting_for_human` — lease exists but operator has not entered the GUI yet.
- `human_active` — operator owns the GUI.
- `completed` — operator finished and automation may continue.
- `cancelled` — operator abandoned the handoff.
- `expired` — TTL elapsed; fail-safe cleanup state.

Only one open (`waiting_for_human` / `human_active`) lease is permitted per
single-operator installation.

Default TTL is five minutes; the API/store accepts 30–1800 seconds. TTL expiry
prevents a lost browser tab from pausing automation indefinitely.

Web completion/cancellation uses a small close-request coordination file rather
than adding a permissive transitional database state. The lease deliberately
stays open, so `takeover_blocks_automation()` continues to pause the Agent,
until the systemd sidecar has stopped the remote desktop and verified that no
Tailscale Serve marker or local VNC/noVNC process remains. Only then does the
sidecar call `complete()` or `cancel()`.

## CLI flow

Start the coordination lease:

```bash
python scripts/handoffctl.py start --reason payment --task-id <computer-task-id> --ttl 300
```

The start command pauses new computer-use and screenshot claims, discards
queued work planned against the old screen, and waits for an already claimed
action or screenshot to finish before it returns. Do not open the remote
desktop until this command returns successfully.

Start the graphical transport:

```bash
bash scripts/novnc_handoff.sh start
```

The helper refuses to start without an open takeover lease. It uses the
current `DISPLAY` when set (otherwise `:0`) and `XAUTHORITY` when set; an
explicit auth file can be supplied with `CONVEYOR_HANDOFF_XAUTHORITY`. The
helper monitors the lease and closes the transport shortly before its TTL.
Both listeners remain hard-coded to `127.0.0.1`.

From the operator's own computer, create an SSH tunnel:

```bash
ssh -L 6080:127.0.0.1:6080 <user>@<vps>
```

Open:

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
```

### Phone access with Tailscale Serve (opt-in)

Install Tailscale on the VPS and phone, sign both into the same tailnet, enable
MagicDNS and HTTPS certificates, and restrict tailnet access to the VPS's
`8443` port to the operator's identity. Do not enable Funnel for this route.
On Linux, the non-root user running the helper must be allowed to manage
Tailscale Serve (for example, `sudo tailscale set --operator=ubuntu` for an
Ubuntu user). Configure this before opening the lease; the helper will fail
closed if Serve cannot be started.

Tailscale Serve proxies HTTPS from the tailnet to the existing
`127.0.0.1:6080` listener; VNC `5901` remains loopback-only. The VNC password
is still required, and must be entered privately on the phone. No SSH tunnel
needs to remain open for the phone connection.

After `handoffctl.py start` has returned, opt in for this lease:

```bash
CONVEYOR_HANDOFF_TAILSCALE_SERVE=1 bash scripts/novnc_handoff.sh start
```

The helper prints the actual `https://<vps>.<tailnet>.ts.net:8443/vnc.html`
address. With Tailscale connected on the phone, open it in Safari. Port `8443`
is reserved for this handoff: startup refuses to overwrite any existing Serve
route there. The helper uses a foreground Serve session and verifies that the
route is removed on stop or shortly before lease expiry. It leaves other Serve
routes alone. If route removal cannot be verified, it stops VNC/noVNC and
blocks lease completion until `bash scripts/novnc_handoff.sh stop` successfully
cleans the stale route.

Never use `tailscale funnel`, put a VNC password in a URL, or allow the entire
tailnet access to the handoff port when other members/devices are present.
The operator's phone must remain connected to Tailscale while using noVNC.
Before treating this route as production-ready, test from the actual phone on
cellular data: load the page, verify the noVNC WebSocket remains connected for
several minutes, perform a harmless tap and keyboard input, then stop the
handoff and confirm the phone loses access. Check from outside the tailnet
that `8443` is unreachable, alongside public `5901` and `6080`.

Mark the lease active after entering:

```bash
python scripts/handoffctl.py activate <takeover-id>
```

After the human-only step is complete:

```bash
bash scripts/novnc_handoff.sh stop
python scripts/handoffctl.py complete <takeover-id>
```

Stop the VNC/noVNC transport before completing or cancelling the lease. The
CLI refuses to release the lease while either listener process is still live
or a Tailscale Serve route still needs cleanup, so the Agent cannot resume
while the operator still owns the desktop.

The noVNC helper creates an ephemeral VNC password in a mode-0700 runtime
directory and deletes the password/auth files on stop. Conveyor's takeover
database never stores that password, and the helper does not print it during
startup. If an operator needs the credential, retrieve it only in a private
VPS terminal that is not being recorded; never place it in chat, shell command
arguments, screenshots, clipboard, or a report.

## Web Workbench flow

The Web Workbench exposes the same safety contract through authenticated APIs
and a dedicated `Human takeover` inspector card:

1. **Take over** calls `POST /api/takeover/start`. Conveyor opens the lease,
   cancels stale pending computer/screenshot work, waits for in-flight work to
   finish, and immediately shows `Privacy mode — Agent paused`.
2. `conveyor-handoff.service` notices the open lease and starts the fixed
   `scripts/novnc_handoff.sh` transport. The Web process never receives
   permission to run arbitrary shell commands.
3. **Open Remote Desktop** opens the configured/private handoff route in a new
   browser tab. It is never embedded in the Workbench and no VNC password is
   placed in the URL or Web API response.
4. Opening the remote desktop marks the coordination lease `human_active`.
5. **Done, resume Agent** or **Cancel handoff** writes a close request. The
   lease remains open and Privacy Mode remains active while the sidecar removes
   Tailscale Serve and local noVNC/x11vnc access.
6. Only after cleanup is verified does the sidecar transition the lease to
   `completed` or `cancelled`. The computer-use loop can then obtain a fresh
   post-handoff observation before continuing.

Authenticated endpoints:

```text
GET  /api/takeover/status
POST /api/takeover/start
POST /api/takeover/activate
POST /api/takeover/complete
POST /api/takeover/cancel
```

The Web bearer token and the VNC credential are separate secrets. Do not place
VNC passwords in URLs, browser history, analytics, SSE events, transcripts, or
server logs.

## Sidecar deployment

`conveyor-handoff.service` is installed and enabled by the standard installer.
It runs as the same configured service user as the rest of Conveyor and owns
only the fixed takeover transport helper. `conveyor status`, `conveyor logs
handoff`, and `sudo conveyor restart handoff` expose its operational state.

The optional `CONVEYOR_HANDOFF_WEB_URL` may provide an already-authenticated,
private HTTPS entrypoint to the handoff page. It is metadata only; never embed
a password, token, userinfo, or other secret in that URL. When Tailscale Serve
is enabled, the sidecar instead uses the verified HTTPS URL printed by the
existing helper.

Operational prerequisites remain unchanged: the VPS needs the validated X11
session, x11vnc/websockify/noVNC tooling, and a readable XAUTHORITY setup. Phone
access additionally requires the previously validated Tailscale configuration.
Do not expose VNC/noVNC ports publicly as a shortcut.
