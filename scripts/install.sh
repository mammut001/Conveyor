#!/usr/bin/env bash
# scripts/install.sh — install/update/uninstall Conveyor from a checked-out source tree.
# The network-facing entrypoint is scripts/bootstrap.sh.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

CONVEYOR_DIR="${CONVEYOR_DIR:-/opt/conveyor}"
CONVEYOR_INSTALL_REF="${CONVEYOR_INSTALL_REF:-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UPDATE_ONLY=false
NO_TAILSCALE="${CONVEYOR_NO_TAILSCALE:-0}"

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

check_root() {
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        log_err "This script must be run as root (use sudo)."
        exit 1
    fi
}

detect_service_identity() {
    local candidate="${CONVEYOR_USER:-}"
    if [[ -z "$candidate" && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        candidate="$SUDO_USER"
    fi
    if [[ -z "$candidate" ]]; then
        candidate="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 && $7 !~ /(nologin|false)$/ { print $1; exit }')"
    fi
    if [[ -z "$candidate" ]]; then
        candidate="root"
        log_warn "No non-root login user detected; services will run as root. Set CONVEYOR_USER to override."
    fi
    if ! id "$candidate" >/dev/null 2>&1; then
        log_err "Service user does not exist: $candidate"
        exit 1
    fi
    CONVEYOR_USER="$candidate"
    CONVEYOR_GROUP="${CONVEYOR_GROUP:-$(id -gn "$CONVEYOR_USER")}"
}

user_home() {
    getent passwd "$CONVEYOR_USER" | awk -F: '{print $6}'
}

install_system_deps() {
    if ! command -v apt-get >/dev/null 2>&1; then
        log_err "This installer currently supports Debian/Ubuntu hosts with apt-get."
        exit 1
    fi
    log_info "Installing system dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv git rsync jq curl ca-certificates make sudo
    log_ok "System dependencies installed"
}

check_deps() {
    local missing=()
    for cmd in python3 git rsync systemctl curl make; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_err "Missing dependencies after package installation: ${missing[*]}"
        exit 1
    fi
}

install_tailscale() {
    if [[ "${NO_TAILSCALE:-0}" == "1" ]]; then
        log_info "Tailscale installation skipped (--no-tailscale or CONVEYOR_NO_TAILSCALE=1)."
        return 0
    fi
    if command -v tailscale >/dev/null 2>&1; then
        log_ok "Tailscale is already installed ($(tailscale version 2>/dev/null | head -n1 || echo 'present'))"
    else
        log_info "Installing Tailscale for mobile & tailnet access..."
        if curl -fsSL https://tailscale.com/install.sh | sh; then
            log_ok "Tailscale installed"
        else
            log_warn "Tailscale installation script exited non-zero; continuing without Tailscale."
            return 0
        fi
    fi

    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now tailscaled >/dev/null 2>&1 || true
    fi

    if [[ -n "${CONVEYOR_USER:-}" && "${CONVEYOR_USER}" != "root" ]] && command -v tailscale >/dev/null 2>&1; then
        tailscale set --operator="${CONVEYOR_USER}" >/dev/null 2>&1 || true
    fi
}

setup_directory() {
    local home
    home="$(user_home)"
    [[ -n "$home" ]] || { log_err "Could not determine home for $CONVEYOR_USER"; exit 1; }

    log_info "Setting up $CONVEYOR_DIR for $CONVEYOR_USER..."
    mkdir -p "$CONVEYOR_DIR" "$home/.codex" "$home/.local/share/conveyor"
    chown "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR"
    chown -R "$CONVEYOR_USER:$CONVEYOR_GROUP" "$home/.codex" "$home/.local/share/conveyor"
    chmod 700 "$home/.codex" "$home/.local/share/conveyor"
}

sync_source() {
    local source_real target_real
    source_real="$(readlink -f "$PROJECT_ROOT")"
    target_real="$(readlink -f "$CONVEYOR_DIR")"
    if [[ "$source_real" == "$target_real" ]]; then
        log_info "Source already lives at $CONVEYOR_DIR; skipping rsync."
        return
    fi

    log_info "Syncing source code to $CONVEYOR_DIR..."
    rsync -a --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='node_modules' \
        --exclude='logs' \
        --exclude='worktrees' \
        --exclude='snapshots' \
        --exclude='state' \
        --exclude='MEMORY.md' \
        --exclude='MEMORY.md.archived-*' \
        "$PROJECT_ROOT/" "$CONVEYOR_DIR/"
    chown -R "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR"
    log_ok "Source synced"
}

setup_venv() {
    log_info "Setting up Python virtual environment..."
    if [[ ! -d "$CONVEYOR_DIR/.venv" ]]; then
        python3 -m venv "$CONVEYOR_DIR/.venv"
    fi
    "$CONVEYOR_DIR/.venv/bin/pip" install --upgrade pip -q
    "$CONVEYOR_DIR/.venv/bin/pip" install -r "$CONVEYOR_DIR/requirements.txt" -q
    chown -R "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR/.venv"
    log_ok "Python dependencies installed"
}

run_as_service_user() {
    local home
    home="$(user_home)"
    sudo -H -u "$CONVEYOR_USER" env HOME="$home" CONVEYOR_DIR="$CONVEYOR_DIR" "$@"
}

setup_env() {
    if [[ -f "$CONVEYOR_DIR/.env" ]]; then
        log_info ".env already exists; keeping current configuration."
        return
    fi
    if [[ "$UPDATE_ONLY" == "true" ]]; then
        log_err ".env is missing; run a full install before --update."
        exit 1
    fi

    log_info "Starting interactive Conveyor configuration..."
    if [[ ! -r /dev/tty ]]; then
        log_err "Interactive configuration requires a TTY. SSH into the host and rerun the installer."
        exit 1
    fi
    run_as_service_user "$CONVEYOR_DIR/.venv/bin/python" "$CONVEYOR_DIR/scripts/configure_env.py" </dev/tty
    log_ok "Configuration saved"
}

install_systemd_units() {
    log_info "Installing systemd units..."
    local home unit src dst
    home="$(user_home)"
    local units=(
        conveyor-telegram-bot.service
        conveyor-feishu-bot.service
        conveyor-desktop-agent.service
        conveyor-web.service
        conveyor-maintain.service
        conveyor-maintain.timer
        conveyor-scheduler.service
        conveyor-scheduler.timer
    )
    for unit in "${units[@]}"; do
        src="$CONVEYOR_DIR/systemd/$unit"
        dst="/etc/systemd/system/$unit"
        if [[ -f "$src" ]]; then
            sed \
              -e "s|/opt/conveyor|$CONVEYOR_DIR|g" \
              -e "s|User=ubuntu|User=$CONVEYOR_USER|g" \
              -e "s|Group=ubuntu|Group=$CONVEYOR_GROUP|g" \
              -e "s|/home/ubuntu|$home|g" \
              "$src" > "$dst"
            chmod 0644 "$dst"
        fi
    done
    systemctl daemon-reload
    log_ok "Systemd units installed"
}

create_default_config() {
    cat > /etc/default/conveyor <<EOF
# Managed by Conveyor installer.
CONVEYOR_DIR=$CONVEYOR_DIR
CONVEYOR_USER=$CONVEYOR_USER
CONVEYOR_INSTALL_REF=$CONVEYOR_INSTALL_REF
EOF
    chmod 0644 /etc/default/conveyor
}

install_cli() {
    install -m 0755 "$CONVEYOR_DIR/scripts/conveyor" /usr/local/bin/conveyor
    log_ok "Installed /usr/local/bin/conveyor"
}

validate_codex() {
    local configured
    configured="$(grep -E '^CODEX_BIN=' "$CONVEYOR_DIR/.env" | tail -1 | cut -d= -f2- || true)"
    if [[ -z "$configured" ]]; then
        log_err "CODEX_BIN is not configured in $CONVEYOR_DIR/.env"
        exit 1
    fi
    if [[ "$configured" == */* ]]; then
        if [[ ! -x "$configured" ]]; then
            log_err "Codex CLI not found or not executable at CODEX_BIN=$configured"
            log_err "Install/authenticate Codex CLI, then run: sudo conveyor configure"
            exit 1
        fi
    elif ! run_as_service_user bash -lc "command -v '$configured' >/dev/null"; then
        log_err "Codex CLI not found in service-user PATH: $configured"
        exit 1
    fi
    log_ok "Codex CLI detected"
}

run_smoke() {
    log_info "Running Conveyor smoke suite..."
    if ! make -C "$CONVEYOR_DIR" PY="$CONVEYOR_DIR/.venv/bin/python" smoke; then
        log_err "Smoke tests failed; services were not restarted."
        exit 1
    fi
    log_ok "Smoke tests passed"
}

enable_services() {
    systemctl enable conveyor-telegram-bot.service >/dev/null
    systemctl enable conveyor-desktop-agent.service >/dev/null
    if grep -Eqi '^CONVEYOR_WEB_ENABLED=(true|1|yes|on)$' "$CONVEYOR_DIR/.env"; then
        systemctl enable conveyor-web.service >/dev/null
    fi
    systemctl enable conveyor-maintain.timer >/dev/null
    systemctl enable conveyor-scheduler.timer >/dev/null
}

start_services() {
    log_info "Starting Conveyor services..."
    systemctl restart conveyor-telegram-bot.service
    systemctl restart conveyor-desktop-agent.service
    if systemctl is-enabled conveyor-web.service >/dev/null 2>&1; then
        systemctl restart conveyor-web.service
    fi
    systemctl restart conveyor-maintain.timer
    systemctl restart conveyor-scheduler.timer
    log_ok "Services started"
}

stop_services() {
    for unit in \
        conveyor-telegram-bot.service conveyor-feishu-bot.service \
        conveyor-desktop-agent.service conveyor-web.service \
        conveyor-maintain.timer conveyor-maintain.service \
        conveyor-scheduler.timer conveyor-scheduler.service; do
        systemctl stop "$unit" 2>/dev/null || true
    done
}

remove_systemd_units() {
    local unit
    for unit in \
        conveyor-telegram-bot.service conveyor-feishu-bot.service \
        conveyor-desktop-agent.service conveyor-web.service \
        conveyor-maintain.service conveyor-maintain.timer \
        conveyor-scheduler.service conveyor-scheduler.timer; do
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
}

print_status() {
    echo
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN} Conveyor installed successfully${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo "  Install dir: $CONVEYOR_DIR"
    echo "  Service user: $CONVEYOR_USER"
    echo "  Source ref: $CONVEYOR_INSTALL_REF"
    echo
    echo "  conveyor status"
    echo "  conveyor logs"
    echo "  conveyor doctor"
    echo "  sudo conveyor update"
    echo
}

do_install() {
    check_root
    install_system_deps
    check_deps
    detect_service_identity
    install_tailscale
    setup_directory
    sync_source
    setup_venv
    setup_env
    install_systemd_units
    create_default_config
    install_cli
    validate_codex
    run_smoke
    enable_services
    start_services
    print_status
}

do_update() {
    UPDATE_ONLY=true
    check_root
    install_system_deps
    check_deps
    detect_service_identity
    install_tailscale
    [[ -f "$CONVEYOR_DIR/.env" ]] || { log_err "No existing Conveyor install found at $CONVEYOR_DIR"; exit 1; }
    sync_source
    setup_venv
    install_systemd_units
    create_default_config
    install_cli
    validate_codex
    run_smoke
    stop_services
    start_services
    log_ok "Update complete"
    /usr/local/bin/conveyor status || true
}

do_uninstall() {
    check_root
    stop_services
    remove_systemd_units
    rm -f /etc/default/conveyor /usr/local/bin/conveyor
    log_warn "Services and CLI removed. Files remain at $CONVEYOR_DIR"
    log_info "To remove files too: rm -rf '$CONVEYOR_DIR'"
}

ACTION="install"
for arg in "$@"; do
    case "$arg" in
        --update) ACTION="update" ;;
        --uninstall) ACTION="uninstall" ;;
        --no-tailscale) NO_TAILSCALE=1 ;;
        --help|-h) ACTION="help" ;;
    esac
done

case "$ACTION" in
    update) do_update ;;
    uninstall) do_uninstall ;;
    help)
        cat <<EOF
Usage: bash scripts/install.sh [--update|--uninstall|--no-tailscale]

Environment:
  CONVEYOR_DIR          install path (default: /opt/conveyor)
  CONVEYOR_USER         service user (auto-detected)
  CONVEYOR_GROUP        service group (auto-detected)
  CONVEYOR_INSTALL_REF  source tag/branch/sha for updates (default: main)
  CONVEYOR_NO_TAILSCALE set to 1 to skip Tailscale installation
EOF
        ;;
    *) do_install ;;
esac
