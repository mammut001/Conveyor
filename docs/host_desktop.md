# Host desktop

The desktop the default agent works on — and the one the Live screen shows
when no agent with its own desktop is selected — can be run by systemd, so it
is there after every boot.

Without this, a headless Linux host only has a desktop after somebody logs in
over RDP, and it is gone again after a reboot: computer use, the desktop chat
window and the Live screen then stay down until the next login.

## Install

```bash
sudo apt-get install -y xserver-xorg-video-dummy
sudo install -D -m 0644 deploy/host-desktop-xorg.conf /etc/X11/conveyor/xorg.conf
sudo install -m 0644 systemd/conveyor-host-desktop.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now conveyor-host-desktop.service
```

Settings live in the unit (override them in `/etc/default/conveyor-host-desktop`):

| Variable | Default | Meaning |
|---|---|---|
| `CONVEYOR_HOST_DISPLAY` | `9` | X display number. Below 10, so it never collides with xrdp's own sessions |
| `CONVEYOR_HOST_DESKTOP_SIZE` | `1600x900` | One of the modes in the X configuration |

## How it works

`scripts/run-host-desktop.sh` starts a headless X server (the `dummy` video
driver) as the service user, sets the size, and runs the user's normal desktop
session (`~/.xsession`, or `xfce4-session`). When the session ends the script
exits and systemd starts it again.

- **Input** is hot-plugged (`AutoAddDevices`), so the kernel `uinput` devices
  that computer use creates reach this X server exactly as they reached the
  xrdp one. The service user must be in the `input` group
  (`scripts/setup-vps-uinput.sh` does that).
- **Other services find it by themselves**: the computer-use clicker, the
  desktop chat window and the Live screen all look for the user's running
  desktop session; they reconnect within seconds of it appearing.
- **Size can be changed live** with `xrandr -s 1440x900` on that display.
- **Deploys do not restart it.** Restarting the unit closes every window.
- **RDP still works, but as a second desktop.** If a desktop session of the
  user is already running when the unit starts (somebody logged in over RDP
  first), the unit waits for it to end instead of creating a second one. An
  RDP login made *after* the unit is up gets its own separate xrdp desktop;
  Conveyor keeps using the one from this unit. Use the Live screen to see and
  drive the host desktop.

The unit is not sandboxed like Conveyor's other units: everything on the
desktop runs inside it, including browsers that start through a setuid helper
and keep their profile in the home directory.

Agents with their own desktop are unaffected; those are kept by
`conveyor-agent-desktops.service` (see [agents.md](agents.md)). An agent task
never falls back to this host display: `X11ComputerBackend` is selected from
the task. When a step asks for a browser, `X11Desktop` uses
`LinuxBrowserController` with that display's own environment (DISPLAY and
XAUTHORITY only). An empty desktop still asks the agent supervisor through
the existing `want_browser` file, and that request is skipped when browser
policy blocks it or the private display does not answer. There is no host
fallback.

## Host browser launch

Computer use on this display may focus or start a browser. That launcher is
not a shell and not a model-supplied command.

- Policy (allow and block lists, including aliases such as `chrome` → Google Chrome) is applied before any focus or launch.
- An existing window is used even when it is minimized. Its WM_CLASS and the owning process must both match a known browser before it is mapped or activated. Focus is accepted only when that process is the foreground window.
- Process identity prefers `/proc/<pid>/exe`. `comm` is only 15 bytes, so a truncated name counts only when it is a unique prefix of one known basename. Substring matches are rejected.
- The executable must be root-owned, not group- or world-writable, and live under a system directory (`/usr/bin`, `/usr/lib`, `/snap/bin`, `/opt/google/chrome`, `/opt/chromium`). `PATH` is not searched.
- Firefox's profile directory includes the DISPLAY. A `/usr/bin/firefox` script that hands off to snap uses `~/snap/firefox/common/` so the snap can write it. Other browsers use `~/.local/share/conveyor/host-browser/<display>/`.
- If `DISPLAY` is unset, or `XAUTHORITY` is missing, or the X server does not answer `getdisplaygeometry`, the controller returns an error and does not start a browser.
- Typing and hotkeys on Linux are sent only while the verified window is still mapped and foreground. macOS still delivers keys to the AX pid in the background.
- A repeated click that leaves the same window and the same pixels gets one different recovery (a plain observation, or one verified browser focus). If that is still stuck, the task stops. A different action with the same image is not a stall. A missing screenshot, a stale pre-action screenshot, an `about:blank` title, or a browser error-page title cannot be reported as a finished webpage. The loop does not read the image to decide whether the page content is correct.

On a machine with Xvfb and xdotool, `python -m unittest tests.test_linux_browser_x11_integration` checks the controller against a private X server. The test starts its own Xvfb and does not attach to the host display.

## Opt-in Linux browser loop on a private display

`scripts/linux_browser_e2e.py` is not part of unit tests or deploy. A supervisor runs it on an already-started private X server and the already-served loopback pages. The harness does not start X, does not listen on a port, and does not edit `/opt/conveyor/.env`. It loads that file for provider settings, then points workspace, task, memory, and screenshot roots at the test directory.

`session.json` names the private display and cookie, for example `{"display": 109, "xauthority": "/tmp/conveyor-pr102-vps/Xauthority"}`. Display `:0` and `:1` are refused.

```bash
python scripts/linux_browser_e2e.py \
  --root /tmp/conveyor-pr102-vps \
  --cases startup/local/weather/stability \
  --rounds 20 \
  --manifest /tmp/conveyor-pr102-vps/session.json
```

Pages are `http://127.0.0.1:19202/index.html` and `run-01.html` through `run-20.html`. Expected codes live only in `/tmp/conveyor-pr102-vps/expected.json` (outside the model workspace). The weather case is marked for a human screenshot review. Results are written under the test root.
