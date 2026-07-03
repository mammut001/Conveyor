#!/usr/bin/env bash
# scripts/install-launchagents.sh
#
# Install (or uninstall) the Conveyor SSH-tunnel LaunchAgent on macOS:
#
#   com.conveyor.ssh-tunnel  — `ssh -N -L <port>:127.0.0.1:<port> <host>`
#
# The tunnel plist uses RunAtLoad + KeepAlive + ThrottleInterval so the OS
# auto-starts it at login and restarts it after a network hiccup.
#
# NOTE: The desktop agent itself is NOT launchd-managed. Because macOS 26 blocks
# launchd-spawned CLI binaries from the TCC-protected ~/Documents folder, the
# agent is spawned and supervised by the ConveyorAgent menu bar app (an .app
# bundle that can be granted Full Disk Access + Screen Recording). This script
# therefore only deals with the SSH tunnel. The desktop-agent plist is still
# shipped as a template but is not installed; any stale installed copy is
# removed by --uninstall.
#
# Usage:
#   scripts/install-launchagents.sh                 # install / refresh tunnel
#   scripts/install-launchagents.sh --uninstall     # unload + remove tunnel plist
#   scripts/install-launchagents.sh --status        # show launchctl state
#
# Overrides (env vars):
#   CONVEYOR_DIR      (default: repo root)
#   CONVEYOR_SSH_HOST (default: vps-oracle, must be in ~/.ssh/config)
#   CONVEYOR_LOCAL_PORT (default: 8766)
set -euo pipefail

ACTION="install"
for arg in "$@"; do
    case "$arg" in
        --uninstall|--remove) ACTION="uninstall" ;;
        --status)             ACTION="status" ;;
        --help|-h)
            sed -n '2,18p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

CONVEYOR_DIR="${CONVEYOR_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SSH_HOST="${CONVEYOR_SSH_HOST:-vps-oracle}"
LOCAL_PORT="${CONVEYOR_LOCAL_PORT:-8766}"

LAUNCH_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
TPL_DIR="$CONVEYOR_DIR/scripts/launchagents"

SSH_PLIST="$LAUNCH_DIR/com.conveyor.ssh-tunnel.plist"
SSH_LABEL="com.conveyor.ssh-tunnel"
# Legacy: the desktop-agent used to be launchd-managed. Keep its paths for
# migration cleanup in --uninstall only.
LEGACY_AGENT_PLIST="$LAUNCH_DIR/com.conveyor.desktop-agent.plist"
LEGACY_AGENT_LABEL="com.conveyor.desktop-agent"

log() { echo "[launchagents] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "this script is for macOS only."
[[ -d "$TPL_DIR" ]]          || die "template dir not found: $TPL_DIR"

render() {
    # render <template> <output>
    local tpl="$1" out="$2"
    sed \
        -e "s|__CONVEYOR_DIR__|$CONVEYOR_DIR|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__SSH_HOST__|$SSH_HOST|g" \
        -e "s|__LOCAL_PORT__|$LOCAL_PORT|g" \
        "$tpl" > "$out"
}

unload_if_loaded() {
    local label="$1" plist="$2"
    if launchctl list "$label" &>/dev/null; then
        log "unloading $label"
        launchctl unload "$plist" 2>/dev/null || true
    fi
}

validate_plist() {
    local plist="$1"
    plutil -lint "$plist" >/dev/null || die "invalid plist: $plist"
}

check_prereqs() {
    [[ -d "$CONVEYOR_DIR/.venv" ]] || die ".venv not found at $CONVEYOR_DIR/.venv"
    ssh -G "$SSH_HOST" &>/dev/null || \
        die "ssh host '$SSH_HOST' not resolvable. Add it to ~/.ssh/config or set CONVEYOR_SSH_HOST."
    log "prereqs ok: dir=$CONVEYOR_DIR host=$SSH_HOST port=$LOCAL_PORT"
}

do_install() {
    check_prereqs
    mkdir -p "$LAUNCH_DIR" "$LOG_DIR"

    # Migrate: remove any stale launchd-managed desktop-agent plist (the agent
    # is now supervised by the menu bar app).
    if [[ -f "$LEGACY_AGENT_PLIST" ]]; then
        unload_if_loaded "$LEGACY_AGENT_LABEL" "$LEGACY_AGENT_PLIST"
        rm -f "$LEGACY_AGENT_PLIST"
        log "removed legacy $LEGACY_AGENT_PLIST (agent now app-managed)"
    fi

    unload_if_loaded "$SSH_LABEL" "$SSH_PLIST"

    log "rendering plist -> $SSH_PLIST"
    render "$TPL_DIR/com.conveyor.ssh-tunnel.plist.template" "$SSH_PLIST"
    validate_plist "$SSH_PLIST"

    log "loading $SSH_LABEL"
    launchctl load "$SSH_PLIST"

    log "installed. ssh-tunnel log: $LOG_DIR/conveyor-ssh-tunnel.log"
    log "desktop agent is supervised by ConveyorAgent.app (grant it Full Disk Access + Screen Recording)."
    log "status: $0 --status"
}

do_uninstall() {
    unload_if_loaded "$SSH_LABEL" "$SSH_PLIST"
    rm -f "$SSH_PLIST"
    log "removed $SSH_PLIST"
    # Also clean up legacy desktop-agent plist if present.
    if [[ -f "$LEGACY_AGENT_PLIST" ]]; then
        unload_if_loaded "$LEGACY_AGENT_LABEL" "$LEGACY_AGENT_PLIST"
        rm -f "$LEGACY_AGENT_PLIST"
        log "removed legacy $LEGACY_AGENT_PLIST"
    fi
}

do_status() {
    if launchctl list "$SSH_LABEL" &>/dev/null; then
        local line
        line="$(launchctl list "$SSH_LABEL")"
        local pid status
        pid="$(awk -F'\t' '/"PID"/ {gsub(/[^0-9]/,"",$3); print $3}' <<<"$line" | head -1)"
        status="$(awk -F'\t' '/"LastExitStatus"/ {gsub(/[^0-9]/,"",$3); print $3}' <<<"$line" | head -1)"
        printf "  %-30s RUNNING  pid=%s lastexit=%s\n" "$SSH_LABEL" "${pid:-?}" "${status:-0}"
    else
        printf "  %-30s STOPPED\n" "$SSH_LABEL"
    fi
    printf "  %-30s (managed by ConveyorAgent.app, not launchd)\n" "$LEGACY_AGENT_LABEL"
}

case "$ACTION" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
esac
