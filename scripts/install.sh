#!/usr/bin/env bash
# scripts/install.sh — One-click install/update for Conveyor
#
# Usage:
#   bash scripts/install.sh              # interactive install
#   bash scripts/install.sh --update     # update only (skip .env prompt)
#   bash scripts/install.sh --uninstall  # remove services and files
#
# Environment variables (can be set before running):
#   CONVEYOR_DIR       — install path (default: /opt/conveyor)
#   CONVEYOR_USER      — service user (default: ubuntu)
#   CONVEYOR_GROUP     — service group (default: ubuntu)

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Config
CONVEYOR_DIR="${CONVEYOR_DIR:-/opt/conveyor}"
CONVEYOR_USER="${CONVEYOR_USER:-ubuntu}"
CONVEYOR_GROUP="${CONVEYOR_GROUP:-ubuntu}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err() { echo -e "${RED}[ERROR]${NC} $1"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_err "This script must be run as root (use sudo)"
        exit 1
    fi
}

check_deps() {
    local missing=()
    for cmd in python3 pip3 git rsync systemctl; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_err "Missing dependencies: ${missing[*]}"
        log_info "Install with: sudo apt install -y python3 python3-pip git rsync"
        exit 1
    fi
}

install_system_deps() {
    log_info "Installing system dependencies..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv git rsync jq
    log_ok "System dependencies installed"
}

setup_directory() {
    log_info "Setting up $CONVEYOR_DIR..."
    mkdir -p "$CONVEYOR_DIR"
    chown "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR"
}

sync_source() {
    log_info "Syncing source code..."
    rsync -a --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        --exclude='node_modules' \
        "$PROJECT_ROOT/" "$CONVEYOR_DIR/"
    chown -R "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR"
    log_ok "Source synced to $CONVEYOR_DIR"
}

setup_venv() {
    log_info "Setting up Python virtual environment..."
    if [[ ! -d "$CONVEYOR_DIR/.venv" ]]; then
        python3 -m venv "$CONVEYOR_DIR/.venv"
        log_ok "Created .venv"
    else
        log_info ".venv already exists, updating..."
    fi
    "$CONVEYOR_DIR/.venv/bin/pip" install --upgrade pip -q
    if [[ -f "$CONVEYOR_DIR/requirements.txt" ]]; then
        "$CONVEYOR_DIR/.venv/bin/pip" install -r "$CONVEYOR_DIR/requirements.txt" -q
        log_ok "Dependencies installed"
    else
        log_warn "No requirements.txt found, skipping pip install"
    fi
}

setup_env() {
    if [[ -f "$CONVEYOR_DIR/.env" ]]; then
        log_info ".env already exists, skipping..."
        return
    fi
    if [[ "$UPDATE_ONLY" == "true" ]]; then
        log_warn ".env not found, run without --update to configure"
        return
    fi
    log_info "Configuring .env..."
    cp "$CONVEYOR_DIR/.env.example" "$CONVEYOR_DIR/.env"
    chown "$CONVEYOR_USER:$CONVEYOR_GROUP" "$CONVEYOR_DIR/.env"
    chmod 600 "$CONVEYOR_DIR/.env"
    log_ok "Created .env from .env.example"
    echo ""
    log_warn "Please edit $CONVEYOR_DIR/.env with your credentials:"
    echo "  1. TELEGRAM_BOT_TOKEN — from @BotFather"
    echo "  2. TELEGRAM_ALLOWED_USER_ID — your Telegram user ID"
    echo "  3. CODEX_WORKSPACE_ROOT — your git repo path"
    echo ""
    read -rp "Press Enter to continue after editing .env..."
}

install_systemd_units() {
    log_info "Installing systemd units..."
    local units=(
        "conveyor-telegram-bot.service"
        "conveyor-feishu-bot.service"
        "conveyor-maintain.service"
        "conveyor-maintain.timer"
        "conveyor-scheduler.service"
        "conveyor-scheduler.timer"
    )
    for unit in "${units[@]}"; do
        local src="$CONVEYOR_DIR/systemd/$unit"
        local dst="/etc/systemd/system/$unit"
        if [[ -f "$src" ]]; then
            # Replace /opt/conveyor with actual CONVEYOR_DIR
            sed "s|/opt/conveyor|$CONVEYOR_DIR|g; s|User=ubuntu|User=$CONVEYOR_USER|g; s|Group=ubuntu|Group=$CONVEYOR_GROUP|g" "$src" > "$dst"
            log_ok "Installed $unit"
        fi
    done
    systemctl daemon-reload
    log_ok "Systemd units installed"
}

create_default_config() {
    log_info "Creating /etc/default/conveyor..."
    cat > /etc/default/conveyor <<EOF
# Conveyor environment overrides
CONVEYOR_DIR=$CONVEYOR_DIR
EOF
    log_ok "Created /etc/default/conveyor"
}

enable_services() {
    log_info "Enabling and starting services..."
    systemctl enable conveyor-telegram-bot.service
    systemctl enable conveyor-maintain.timer
    systemctl enable conveyor-scheduler.timer
    log_ok "Services enabled"
}

start_services() {
    log_info "Starting services..."
    systemctl restart conveyor-telegram-bot.service
    systemctl restart conveyor-maintain.timer
    systemctl restart conveyor-scheduler.timer
    log_ok "Services started"
}

stop_services() {
    log_info "Stopping services..."
    systemctl stop conveyor-telegram-bot.service 2>/dev/null || true
    systemctl stop conveyor-feishu-bot.service 2>/dev/null || true
    systemctl stop conveyor-maintain.timer 2>/dev/null || true
    systemctl stop conveyor-maintain.service 2>/dev/null || true
    systemctl stop conveyor-scheduler.timer 2>/dev/null || true
    systemctl stop conveyor-scheduler.service 2>/dev/null || true
    log_ok "Services stopped"
}

remove_systemd_units() {
    log_info "Removing systemd units..."
    local units=(
        "conveyor-telegram-bot.service"
        "conveyor-feishu-bot.service"
        "conveyor-maintain.service"
        "conveyor-maintain.timer"
        "conveyor-scheduler.service"
        "conveyor-scheduler.timer"
    )
    for unit in "${units[@]}"; do
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    log_ok "Systemd units removed"
}

run_smoke() {
    log_info "Running smoke tests..."
    if "$CONVEYOR_DIR/.venv/bin/python" -m pytest "$CONVEYOR_DIR/scripts/" -x -q 2>/dev/null; then
        log_ok "Smoke tests passed"
    else
        log_warn "Smoke tests failed (non-fatal, services are still running)"
    fi
}

print_status() {
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN} Conveyor installed successfully!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo "  Install dir:  $CONVEYOR_DIR"
    echo "  Services:     conveyor-telegram-bot, conveyor-maintain, conveyor-scheduler"
    echo ""
    echo "  Check status:  systemctl status conveyor-telegram-bot"
    echo "  View logs:     journalctl -u conveyor-telegram-bot -f"
    echo "  Update:        bash scripts/install.sh --update"
    echo ""
    echo "  Open Telegram and send /start to your bot!"
    echo ""
}

do_install() {
    check_root
    check_deps
    install_system_deps
    setup_directory
    sync_source
    setup_venv
    setup_env
    install_systemd_units
    create_default_config
    enable_services
    start_services
    print_status
}

do_update() {
    check_root
    check_deps
    sync_source
    setup_venv
    install_systemd_units
    stop_services
    start_services
    log_ok "Update complete!"
    echo ""
    systemctl status conveyor-telegram-bot.service --no-pager
}

do_uninstall() {
    check_root
    stop_services
    remove_systemd_units
    rm -f /etc/default/conveyor
    log_warn "Services removed. Files remain at $CONVEYOR_DIR"
    log_info "To remove files: rm -rf $CONVEYOR_DIR"
}

# Main
UPDATE_ONLY=false
case "${1:-}" in
    --update)
        UPDATE_ONLY=true
        do_update
        ;;
    --uninstall)
        do_uninstall
        ;;
    --help|-h)
        echo "Usage: bash scripts/install.sh [--update|--uninstall]"
        echo ""
        echo "  (no args)    Full install with interactive .env setup"
        echo "  --update     Update code and restart services"
        echo "  --uninstall  Remove services and systemd units"
        echo ""
        echo "Environment variables:"
        echo "  CONVEYOR_DIR   Install path (default: /opt/conveyor)"
        echo "  CONVEYOR_USER  Service user (default: ubuntu)"
        echo "  CONVEYOR_GROUP Service group (default: ubuntu)"
        ;;
    *)
        do_install
        ;;
esac
