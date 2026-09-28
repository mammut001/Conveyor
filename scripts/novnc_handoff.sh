#!/usr/bin/env bash
set -euo pipefail

# Loopback-only noVNC transport for an existing graphical X11 session.
# This script intentionally does NOT open firewall ports or expose noVNC.
# Reach it through an SSH tunnel, Tailscale/VPN, or a separately secured
# reverse proxy. Conveyor itself never reads the VNC password or keystrokes.

COMMAND="${1:-status}"
DISPLAY_NAME="${CONVEYOR_HANDOFF_DISPLAY:-:0}"
VNC_PORT="${CONVEYOR_HANDOFF_VNC_PORT:-5901}"
NOVNC_PORT="${CONVEYOR_HANDOFF_NOVNC_PORT:-6080}"
LISTEN_HOST="127.0.0.1"
RUNTIME_BASE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ ! -d "$RUNTIME_BASE" || ! -w "$RUNTIME_BASE" ]]; then
  RUNTIME_BASE="/tmp/conveyor-handoff-$(id -u)"
fi
STATE_DIR="${CONVEYOR_HANDOFF_STATE_DIR:-$RUNTIME_BASE/conveyor-handoff}"
X11VNC_PID="$STATE_DIR/x11vnc.pid"
WEBSOCKIFY_PID="$STATE_DIR/websockify.pid"
PASSWORD_FILE="$STATE_DIR/password.txt"
VNC_AUTH_FILE="$STATE_DIR/vnc.pass"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

ensure_state_dir() {
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
}

pid_alive() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

find_novnc_root() {
  if [[ -n "${CONVEYOR_NOVNC_WEB_ROOT:-}" ]]; then
    [[ -f "$CONVEYOR_NOVNC_WEB_ROOT/vnc.html" ]] || die "CONVEYOR_NOVNC_WEB_ROOT has no vnc.html"
    printf '%s\n' "$CONVEYOR_NOVNC_WEB_ROOT"
    return
  fi
  local candidate
  for candidate in /usr/share/novnc /usr/share/novnc/www /opt/novnc; do
    if [[ -f "$candidate/vnc.html" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  die "Could not find noVNC web root. Install noVNC or set CONVEYOR_NOVNC_WEB_ROOT."
}

start_transport() {
  command -v x11vnc >/dev/null 2>&1 || die "x11vnc is required"
  command -v websockify >/dev/null 2>&1 || die "websockify is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  ensure_state_dir

  if pid_alive "$X11VNC_PID" || pid_alive "$WEBSOCKIFY_PID"; then
    die "handoff transport is already running; run '$0 status'"
  fi

  local novnc_root password
  novnc_root="$(find_novnc_root)"
  password="$(python3 - <<'PY'
import secrets
alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
print(''.join(secrets.choice(alphabet) for _ in range(8)))
PY
)"
  umask 077
  printf '%s\n' "$password" > "$PASSWORD_FILE"
  x11vnc -storepasswd "$password" "$VNC_AUTH_FILE" >/dev/null
  chmod 600 "$PASSWORD_FILE" "$VNC_AUTH_FILE"

  log "Starting x11vnc for display $DISPLAY_NAME on loopback:$VNC_PORT"
  x11vnc \
    -display "$DISPLAY_NAME" \
    -rfbauth "$VNC_AUTH_FILE" \
    -rfbport "$VNC_PORT" \
    -localhost \
    -forever \
    -shared \
    -noxdamage \
    -bg \
    -o /dev/null \
    -pidfile "$X11VNC_PID" >/dev/null 2>&1

  log "Starting noVNC/websockify on loopback:$NOVNC_PORT"
  websockify \
    --web "$novnc_root" \
    --daemon \
    --pidfile="$WEBSOCKIFY_PID" \
    "$LISTEN_HOST:$NOVNC_PORT" \
    "$LISTEN_HOST:$VNC_PORT"

  cat <<EOF

Human handoff transport is ready.

Local URL:
  http://127.0.0.1:${NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale

Temporary VNC password:
  ${password}

Recommended access from your own computer:
  ssh -L ${NOVNC_PORT}:127.0.0.1:${NOVNC_PORT} <user>@<vps>

Then open the Local URL in your browser.

IMPORTANT:
  - ports ${VNC_PORT}/${NOVNC_PORT} are loopback-only; do not expose them publicly
  - stop the handoff immediately after sensitive input is complete
  - do not enable screen recording / screenshots while entering secrets or payment data
EOF
}

stop_pidfile() {
  local file="$1"
  if ! [[ -f "$file" ]]; then return 0; fi
  local pid
  pid="$(cat "$file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
}

stop_transport() {
  ensure_state_dir
  stop_pidfile "$WEBSOCKIFY_PID"
  stop_pidfile "$X11VNC_PID"
  rm -f "$PASSWORD_FILE" "$VNC_AUTH_FILE"
  log "Human handoff transport stopped and temporary credentials removed."
}

status_transport() {
  ensure_state_dir
  local vnc=stopped ws=stopped
  pid_alive "$X11VNC_PID" && vnc=running
  pid_alive "$WEBSOCKIFY_PID" && ws=running
  printf 'x11vnc: %s\nwebsockify: %s\nurl: http://127.0.0.1:%s/vnc.html?autoconnect=1&resize=scale\n' "$vnc" "$ws" "$NOVNC_PORT"
}

show_password() {
  [[ -f "$PASSWORD_FILE" ]] || die "no active temporary password"
  cat "$PASSWORD_FILE"
}

case "$COMMAND" in
  start) start_transport ;;
  stop) stop_transport ;;
  status) status_transport ;;
  password) show_password ;;
  *) die "usage: $0 {start|stop|status|password}" ;;
esac
