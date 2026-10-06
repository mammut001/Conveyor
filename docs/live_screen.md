# Live screen

An embedded, live view of the host desktop inside the Web Console, with
one-click takeover. Open the **Computer** section, click the thumbnail, and the
screen opens full size; **Take control** hands you the mouse and keyboard and
pauses the Agent until you release.

Compared with [Secure Human Takeover](human_takeover.md) (noVNC in a separate
tab, its own password, a sidecar, an SSH tunnel or Tailscale route), this is
the low-friction path: it reuses the console's own authenticated HTTP API, so
there is no extra port, credential, or service.

## Enable

```dotenv
CONVEYOR_LIVE_SCREEN_ENABLED=true
```

Host requirements (Linux, X11): ImageMagick `import` for capture and `xdotool`
for input. The display is discovered from the running desktop session
(`xfce4-session`, `gnome-session`, …); set `CONVEYOR_LIVE_SCREEN_DISPLAY=:N`
to pin it.

| Variable | Default | Meaning |
|---|---|---|
| `CONVEYOR_LIVE_SCREEN_ENABLED` | `false` | Master switch |
| `CONVEYOR_LIVE_SCREEN_CONTROL` | `true` | `false` makes it view-only |
| `CONVEYOR_LIVE_SCREEN_FPS` | `4` | Capture rate while someone is watching (0.5–10) |
| `CONVEYOR_LIVE_SCREEN_QUALITY` | `60` | JPEG quality (20–90) |
| `CONVEYOR_LIVE_SCREEN_DISPLAY` | auto | X display to capture |

## How it works

```text
Browser (Web Console, bearer token)
   │  GET  /api/screen/frame?since=N   long-poll, JPEG or 204
   │  POST /api/screen/control         take | release
   │  POST /api/screen/input           pointer / key / text events
   ▼
web_console.py ── live_screen.py ──▶ import -window root   (capture)
                         │        └─▶ xdotool               (input, only with lease)
                         ▼
                 human_takeover.sqlite3  (exclusive GUI lease)
```

- **Viewing** starts a capture loop only while a viewer is polling, and stops
  about ten seconds after the last request. Unchanged frames are not resent.
- While someone is watching, the X idle timer is reset and a running
  screensaver is dismissed (`xset s reset`, `xfce4-screensaver-command
  --deactivate`, …), because an idle headless desktop otherwise shows only
  black. A locked session still shows its unlock prompt.
- **Take control** opens the same exclusive lease as Secure Human Takeover:
  queued computer-use steps are cancelled, an in-flight action is allowed to
  finish, and from then on the computer-use loop and Agent screenshots are
  blocked. If the Agent does not go idle within 20 seconds the takeover is
  refused instead of letting two parties drive the pointer.
- Clicking the picture also takes control; that click is not forwarded.
  After each input the screen is captured immediately and at 10 fps for the
  next second and a half, so the result of a click shows up without waiting
  for the idle frame rate.
- **Release** lifts any held mouse button or modifier, then completes the
  lease. The Agent takes a fresh observation before continuing.

## Safety contract

- Off by default. Enabling it means the web token is enough to see the
  desktop and, with control on, to drive it — keep the console on loopback or
  a private network as before.
- Input is accepted only while this console process holds the lease.
- Frames exist in memory for the authenticated viewer only. They are never
  written to disk, logged, attached to a transcript, or given to the Agent.
- Typed text and key names are never logged; only "control taken/released".
- The lease is 120 seconds, renewed while a viewer is present. Closing the
  tab releases it immediately; a vanished viewer loses it after 45 seconds;
  a crashed console after at most 120.
- Only one takeover exists at a time: live-screen control is refused while a
  noVNC handoff is open, and vice versa.

The computer-use blocked-keyword policy is a rule for the Agent. It does not
apply to what the operator types during a takeover, and nothing typed there
reaches Conveyor state.

## Limits

- X11 only. No audio, no clipboard sync from host to browser (paste into the
  viewer is typed as text).
- The remote pointer is not drawn in the frames; your own cursor shows where
  you are pointing.
- A few frames per second: fine for clicking through a dialog or a login,
  not for video.
