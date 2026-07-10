# Conveyor Agent — macOS Menu Bar App

A lightweight macOS menu bar app that keeps the Conveyor desktop observe stack
alive and visible: it supervises the desktop agent python child process, shows
real-time status for the SSH tunnel + agent, and offers Start/Stop/Restart,
log viewing, Preferences, and state-change notifications.

## Architecture (hybrid)

Two layers, split to play nicely with macOS 26 TCC:

- **SSH tunnel** — managed by **launchd** (`com.conveyor.ssh-tunnel`).
  `RunAtLoad` + `KeepAlive` + `ThrottleInterval` give auto-start-at-login and
  automatic reconnect. No `~/Documents` access needed, so launchd is fine.
  Install via `scripts/install-launchagents.sh`.

- **Desktop agent** — managed by **this app** (`AgentSupervisor`), NOT launchd.
  macOS 26 blocks launchd-spawned CLI binaries from the TCC-protected
  `~/Documents` folder, so the agent is spawned as a child of the signed app
  bundle (which can be granted Full Disk Access + Screen Recording). The
  supervisor watchdog restarts it on crash (5s throttle) and cleans up orphaned
  agents from a previous/crashed app instance on start.

The app itself is a `MenuBarExtra` (`LSUIElement=true` → no Dock icon, menu bar
only). Login-items integration uses `SMAppService.mainApp`.

## Build & install

```bash
# debug build -> build/ConveyorAgent.app
bash menubar-agent/build.sh

# release build + install to /Applications + ad-hoc sign + lsregister
bash menubar-agent/build.sh install
```

Ad-hoc signing (`codesign -s -`) is required — macOS won't show notification
authorization prompts (or reliably offer TCC grants) for unsigned apps.

## First-run setup

1. Configure the agent env + tunnel plist:
   ```bash
   bash scripts/setup-desktop-agent.sh
   ```
   (Creates `.desktop-agent.env`, `run-desktop-agent.sh`, and installs the
   ssh-tunnel LaunchAgent.)

2. Build & install the app:
   ```bash
   bash menubar-agent/build.sh install
   open "/Applications/Conveyor Agent.app"
   ```

3. Grant permissions in **System Settings → Privacy & Security**:
   - **Full Disk Access** → `Conveyor Agent` (so the spawned agent can read
     `~/Documents/.../Conveyor`).
   - **Screen Recording** → `Conveyor Agent` (so observe requests can capture).
   - **Notifications** → `Conveyor Agent` → Allow, alert style = Banners/Alerts.
     (The app requests authorization on launch; if no prompt appears, enable it
     manually here.)

4. (Optional) In the menu bar app → **Preferences…** → tick **Launch at Login**
   (`SMAppService` registers the app as a login item).

## Menu bar icon

```
🟢 C   both healthy       🟡 C   partial       🔴 C   all down       ⚪️ C   unknown
```

Status is polled every 2s. SSH-tunnel state comes from `launchctl list`;
agent state comes from the in-app `AgentSupervisor`.

## Menu

- Per-service status row (emoji + name + Running/Stopped + uptime)
- VPS host / Node / Control plane URL (read from `.desktop-agent.env`)
- ▶ Start All / ⏹ Stop All / 🔄 Restart All
- 📋 View SSH Tunnel Log / View Agent Log (opens in Console.app)
- ⚙️ Preferences… (SSH host, port, repo dir, launch-at-login, notifications)
- 🔔 Send test notification (diagnostic — bypasses transition detection)
- 🔄 Refresh / Quit

## Notifications

Posted on running↔stopped transitions for each service (`.unknown` is treated
as stopped for transition purposes, so an unload/disappear also fires):

- SSH Tunnel: `Conveyor: SSH Tunnel 下线 — VPS 连接已断开…` / `上线 — 已恢复`
- Desktop Agent: `下线` / `上线` on crash+restart.

Toggle via Preferences → "Show state-change notifications".

## Logs

- SSH tunnel: `~/Library/Logs/conveyor-ssh-tunnel.log`
- Desktop agent: `~/Library/Logs/conveyor-desktop-agent.log`
  (agent stdout/stderr is unbuffered via `python -u` + `PYTHONUNBUFFERED=1` so
  lines stream in real time; the supervisor prepends `[supervisor <ts>]`
  launch/exit lines.)

## Files

```
menubar-agent/
  Package.swift
  build.sh                      # build + ad-hoc sign + install to /Applications
  Resources/Info.plist          # LSUIElement=true
  Sources/ConveyorAgent/
    App.swift                   # MenuBarExtra + HealthMonitor (2s poll) + menu UI
    AgentSupervisor.swift       # spawn python agent + watchdog + orphan cleanup
    StatusChecker.swift         # launchctl list parse + ps uptime
    ProcessController.swift     # hybrid: tunnel via launchctl, agent via supervisor
    Models.swift                # Service / ServiceState / AppHealth
    Config.swift                # reads .desktop-agent.env (sshHost/dir from prefs)
    Preferences.swift           # PreferencesStore (UserDefaults) + window + SMAppService
    Notifications.swift         # UNUserNotificationCenter transition notifications
scripts/
  setup-desktop-agent.sh        # env + run script + tunnel plist install
  install-launchagents.sh       # ssh-tunnel LaunchAgent install/uninstall/status
  launchagents/
    com.conveyor.ssh-tunnel.plist.template
    com.conveyor.desktop-agent.plist.template   # legacy, not installed (app-managed)
```

## Managing the tunnel LaunchAgent

```bash
bash scripts/install-launchagents.sh               # install / refresh
bash scripts/install-launchagents.sh --status      # launchctl state
bash scripts/install-launchagents.sh --uninstall   # unload + remove plist

# overrides
CONVEYOR_SSH_HOST=vps-oracle CONVEYOR_LOCAL_PORT=8766 bash scripts/install-launchagents.sh
```

## Troubleshooting

- **Agent log empty / looks hung** — Python buffers stdout when it's a file.
  The supervisor passes `-u` + `PYTHONUNBUFFERED=1`; if you run the agent
  manually with redirection, add those yourself or you'll see no output.
- **No notification prompt / app not in Notifications list** — app must be
  ad-hoc signed (`build.sh` does this). Rebuild with `build.sh install`.
- **`open` no-ops after killing the app** — LaunchServices caches the running
  state briefly; wait a few seconds, or run the binary directly for testing.
  Normal launch (login item / double-click) is unaffected.
- **Agent can't read `~/Documents`** — grant Full Disk Access to
  `Conveyor Agent` (not to `/bin/bash` — macOS 26 ignores FDA for system
  binaries). The app-managed architecture exists precisely because launchd
  can't get this grant.
- **Duplicate agents after relaunch** — `AgentSupervisor.start()` pkills
  pre-existing desktop-agent poller orphans first; if you see
  duplicates, kill them: `pkill -f "desktop_agent.py --poll-observe"`.
