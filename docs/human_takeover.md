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
completed
    |
    | transport closes + Agent observes the new state
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

During a human takeover, Conveyor must treat the desktop as privacy-sensitive:

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
Operator closes handoff
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

## Current CLI flow

Start the coordination lease:

```bash
python scripts/handoffctl.py start --reason payment --task-id <computer-task-id> --ttl 300
```

Start the graphical transport:

```bash
bash scripts/novnc_handoff.sh start
```

From the operator's own computer, create an SSH tunnel:

```bash
ssh -L 6080:127.0.0.1:6080 <user>@<vps>
```

Open:

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
```

Mark the lease active after entering:

```bash
python scripts/handoffctl.py activate <takeover-id>
```

After the human-only step is complete:

```bash
python scripts/handoffctl.py complete <takeover-id>
bash scripts/novnc_handoff.sh stop
```

The noVNC helper creates an ephemeral VNC password in a mode-0700 runtime
directory and deletes the password/auth files on stop. Conveyor's takeover
database never stores that password.

## Web Workbench integration

This PR intentionally keeps the transport separate from `App.tsx`, because the
Web Primary Interface work is landing independently.

The follow-up Web integration should add a `Human takeover` card with:

1. **Take over** — creates the lease and pauses automation.
2. **Open remote desktop** — opens a configured, authenticated handoff route in
   a new tab; do not iframe an arbitrary VNC origin.
3. A visible `Privacy mode — Agent paused` banner.
4. **Done, resume Agent** — completes the lease, closes/invalidates the remote
   transport, then performs one fresh observe after the sensitive page is gone.
5. **Cancel** — closes the lease without claiming the sensitive step succeeded.

The Web bearer token and the VNC credential are separate secrets. Do not place
VNC passwords in URLs, browser history, analytics, SSE events, transcripts, or
server logs.

## Remaining hardening before exposing this as a one-click Web feature

- Add authenticated Web API endpoints for takeover start/status/complete/cancel.
- Make the noVNC sidecar lifecycle systemd-managed rather than giving the Web
  process privilege to spawn or kill arbitrary commands.
- Put the handoff route behind the same private network / TLS boundary as the
  Web Workbench.
- Add post-handoff one-shot observe only after the takeover closes.
- Add an integration test proving no computer action is created while a
  takeover lease is open.
- Verify x11vnc against the actual VPS display manager/XAUTHORITY setup.

Do not expose the noVNC or VNC port publicly as a shortcut for those steps.
