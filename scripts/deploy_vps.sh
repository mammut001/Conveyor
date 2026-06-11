#!/usr/bin/env bash
# deploy_vps.sh — safe idempotent deploy on the VPS.
#
# Called by GitHub Actions after push to main, or manually:
#   ssh user@host 'bash /opt/conveyor/scripts/deploy_vps.sh'
#
# Environment:
#   CONVEYOR_DEPLOY_PATH  — repo root on VPS (default /opt/conveyor)
#
# Requirements on the VPS:
#   - git repo already cloned at CONVEYOR_DEPLOY_PATH
#   - .env exists only on VPS (never committed)
#   - .venv exists with dependencies installed
#   - systemd services: conveyor-telegram-bot, conveyor-feishu-bot
#   - deploy user has passwordless sudo for systemctl restart/status
#
# Safety:
#   - flock prevents concurrent deploys
#   - make smoke gates the restart
#   - .env is never printed
#   - secrets are never logged
set -euo pipefail

DEPLOY_PATH="${CONVEYOR_DEPLOY_PATH:-/opt/conveyor}"
LOCK_FILE="${DEPLOY_PATH}/.deploy.lock"
LOG_PREFIX="[deploy]"

log() { echo "${LOG_PREFIX} $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---- lock ----------------------------------------------------------------
exec 200>"${LOCK_FILE}"
flock -n 200 || die "Another deploy is already running (lock: ${LOCK_FILE})"

# ---- preflight -----------------------------------------------------------
cd "${DEPLOY_PATH}"
[[ -f .env ]] || die ".env not found at ${DEPLOY_PATH}/.env"
[[ -d .venv ]] || die ".venv not found at ${DEPLOY_PATH}/.venv"
[[ -f scripts/deploy_vps.sh ]] || die "scripts/deploy_vps.sh not found"

OLD_COMMIT="$(git rev-parse --short HEAD)"
log "Current commit: ${OLD_COMMIT}"

# ---- update --------------------------------------------------------------
log "Fetching origin/main..."
git fetch origin main --quiet

TARGET_COMMIT="$(git rev-parse --short origin/main)"
log "Target commit:  ${TARGET_COMMIT}"

if [[ "${OLD_COMMIT}" == "${TARGET_COMMIT}" ]]; then
    log "Already up to date; skipping reset."
else
    log "Resetting to origin/main..."
    git reset --hard origin/main --quiet
    # Clean untracked files but preserve .env and .venv
    git clean -fd --exclude=.env --exclude=.venv --quiet
fi

NEW_COMMIT="$(git rev-parse --short HEAD)"
log "Now at:         ${NEW_COMMIT}"

# ---- venv sanity ---------------------------------------------------------
log "Python version: $(.venv/bin/python --version 2>&1)"

# ---- smoke ---------------------------------------------------------------
log "Running smoke tests..."
if ! make smoke; then
    die "Smoke tests FAILED. Services will NOT be restarted."
fi
log "Smoke tests passed."

# ---- restart services ----------------------------------------------------
SERVICES=(
    conveyor-telegram-bot
    conveyor-feishu-bot
)
# Optionally restart maintain timer if it exists
if systemctl list-unit-files conveyor-maintain.timer &>/dev/null; then
    SERVICES+=(conveyor-maintain.timer)
fi

for svc in "${SERVICES[@]}"; do
    log "Restarting ${svc}..."
    sudo systemctl restart "${svc}" || log "WARNING: failed to restart ${svc}"
done

# ---- status summary ------------------------------------------------------
log "Service status:"
for svc in "${SERVICES[@]}"; do
    STATUS="$(systemctl is-active "${svc}" 2>/dev/null || echo 'inactive')"
    log "  ${svc}: ${STATUS}"
done

log "Deploy complete: ${OLD_COMMIT} → ${NEW_COMMIT}"
