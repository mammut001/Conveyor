#!/usr/bin/env bash
# Minimal network bootstrap for Conveyor.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mammut001/Conveyor/main/scripts/bootstrap.sh | sudo bash
#
# Optional:
#   curl -fsSL .../bootstrap.sh | sudo CONVEYOR_VERSION=v0.3.0 bash
#   curl -fsSL .../bootstrap.sh | sudo CONVEYOR_MODE=update bash
set -euo pipefail

REPO_URL="${CONVEYOR_REPO_URL:-https://github.com/mammut001/Conveyor.git}"
REF="${CONVEYOR_VERSION:-main}"
MODE="${CONVEYOR_MODE:-install}"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  die "Conveyor's one-line installer currently supports Linux hosts only."
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  die "Run as root, for example: curl -fsSL <installer-url> | sudo bash"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  die "The one-line installer currently supports Debian/Ubuntu hosts with apt-get."
fi

if ! command -v git >/dev/null 2>&1; then
  log "Installing git for bootstrap..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git ca-certificates
fi

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

log "Downloading Conveyor (${REF})..."
git clone --filter=blob:none --quiet "$REPO_URL" "$TMP_DIR/conveyor"

if ! git -C "$TMP_DIR/conveyor" checkout --quiet --detach "$REF" 2>/dev/null; then
  if git -C "$TMP_DIR/conveyor" rev-parse --verify --quiet "origin/$REF" >/dev/null; then
    git -C "$TMP_DIR/conveyor" checkout --quiet --detach "origin/$REF"
  else
    die "Could not resolve Conveyor version/ref: $REF"
  fi
fi

INSTALL_ARGS=()
case "$MODE" in
  install) ;;
  update) INSTALL_ARGS+=(--update) ;;
  uninstall) INSTALL_ARGS+=(--uninstall) ;;
  *) die "Unsupported CONVEYOR_MODE: $MODE" ;;
esac

log "Starting Conveyor installer..."
CONVEYOR_INSTALL_REF="$REF" \
  bash "$TMP_DIR/conveyor/scripts/install.sh" "${INSTALL_ARGS[@]}" </dev/tty
