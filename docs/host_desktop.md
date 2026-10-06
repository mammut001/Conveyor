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
`conveyor-agent-desktops.service` (see [agents.md](agents.md)).
