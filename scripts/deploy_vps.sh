#!/usr/bin/env bash
# deploy_vps.sh — safe idempotent deploy on the VPS (git-based).
#
# Called by GitHub Actions after push to main, or manually:
#   ssh user@host 'bash /opt/conveyor/scripts/deploy_vps.sh'
#
# Environment:
#   CONVEYOR_DEPLOY_PATH  — repo root on VPS (default /opt/conveyor)
#   GITHUB_SHA            — commit SHA from GitHub Actions (optional)
#   GITHUB_REF_NAME       — branch/tag from GitHub Actions (optional)
#   GITHUB_RUN_ID         — run ID from GitHub Actions (optional)
#   DEPLOY_SOURCE         — "github-actions" or "manual" (default)
#
# Requirements on the VPS:
#   - git repo already cloned at CONVEYOR_DEPLOY_PATH
#   - .env exists only on VPS (never committed)
#   - .venv exists; this script syncs dependencies from requirements.txt
#   - systemd services: conveyor-telegram-bot, conveyor-feishu-bot
#   - deploy user has passwordless sudo for systemctl restart/status/is-active
#
# Safety:
#   - flock prevents concurrent deploys
#   - make smoke gates the restart
#   - .env is never printed
#   - secrets are never logged
#   - writes .deploy-status.json for /deploy_status command
set -euo pipefail

DEPLOY_PATH="${CONVEYOR_DEPLOY_PATH:-/opt/conveyor}"
LOCK_FILE="${DEPLOY_PATH}/.deploy.lock"
STATUS_FILE="${DEPLOY_PATH}/.deploy-status.json"
BACKUP_DIR="${DEPLOY_PATH}/.deploy-backups"
LOG_PREFIX="[deploy]"
DEPLOY_SOURCE="${DEPLOY_SOURCE:-manual}"
GIT_SHA="${GITHUB_SHA:-}"
GIT_REF="${GITHUB_REF_NAME:-}"
RUN_ID="${GITHUB_RUN_ID:-}"

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
    # Backup key files before reset
    TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP_PATH="${BACKUP_DIR}/${TIMESTAMP}"
    mkdir -p "${BACKUP_PATH}"
    for f in Makefile config.py runner.py bot.py feishu_bot.py; do
      [[ -f "$f" ]] && cp "$f" "${BACKUP_PATH}/" 2>/dev/null || true
    done
    # Keep only last 5 backups
    if [[ -d "${BACKUP_DIR}" ]]; then
      ls -1dt "${BACKUP_DIR}"/*/ 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true
    fi
    log "Backup at ${BACKUP_PATH}"

    git reset --hard origin/main --quiet
    # Clean untracked files but preserve .env and .venv
    git clean -fd --exclude=.env --exclude=.venv --quiet
fi

NEW_COMMIT="$(git rev-parse --short HEAD)"
log "Now at:         ${NEW_COMMIT}"

# Use git SHA if GITHUB_SHA not provided
if [[ -z "$GIT_SHA" ]]; then
  GIT_SHA="$NEW_COMMIT"
else
  GIT_SHA="$(echo "$GIT_SHA" | head -c 7)"
fi

# ---- venv sanity ---------------------------------------------------------
log "Python version: $(.venv/bin/python --version 2>&1)"

# ---- dependency sync -----------------------------------------------------
[[ -f requirements.txt ]] || die "requirements.txt not found"
log "Syncing Python dependencies from requirements.txt..."
PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 .venv/bin/python -m pip install -r requirements.txt
log "Python dependencies synced."

# ---- smoke ---------------------------------------------------------------
log "Running smoke tests..."
if ! make smoke; then
    die "Smoke tests FAILED. Services will NOT be restarted."
fi
log "Smoke tests passed."

# ---- restart services ----------------------------------------------------
SERVICES=(
    conveyor-telegram-bot.service
    conveyor-feishu-bot.service
)
# Optionally restart maintain timer if it exists
if systemctl list-unit-files conveyor-maintain.timer &>/dev/null; then
    SERVICES+=(conveyor-maintain.timer)
fi

declare -A SVC_STATUS
ALL_ACTIVE=true
for svc in "${SERVICES[@]}"; do
    log "Restarting ${svc}..."
    if ! sudo -n systemctl restart "${svc}"; then
      log "WARNING: restart command for ${svc} returned non-zero; checking final state"
    fi
    sleep 2
    STATE="$(sudo -n systemctl is-active "${svc}" 2>/dev/null || echo 'inactive')"
    SVC_STATUS["$svc"]="$STATE"
    if [[ "$STATE" != "active" ]]; then
      ALL_ACTIVE=false
      log "WARNING: ${svc} is ${STATE} after restart"
    else
      log "  ${svc}: ${STATE}"
    fi
done

# ---- rollback guard (if backed up and services unhealthy) -----------------
if [[ "$ALL_ACTIVE" == "false" && -n "${BACKUP_PATH:-}" && -d "${BACKUP_PATH:-}" ]]; then
  log "Some services are not active. Attempting rollback ..."
  for f in Makefile config.py runner.py bot.py feishu_bot.py; do
    if [[ -f "${BACKUP_PATH}/$f" ]]; then
      cp "${BACKUP_PATH}/$f" "${DEPLOY_PATH}/$f" 2>/dev/null || true
    fi
  done
  for svc in "${SERVICES[@]}"; do
    STATE="${SVC_STATUS[$svc]:-unknown}"
    if [[ "$STATE" != "active" ]]; then
      log "Re-restarting ${svc} after rollback ..."
      sudo -n systemctl restart "${svc}" 2>/dev/null || true
      sleep 2
      NEW_STATE="$(sudo -n systemctl is-active "${svc}" 2>/dev/null || echo 'inactive')"
      SVC_STATUS["$svc"]="$NEW_STATE"
      log "  ${svc}: ${NEW_STATE}"
    fi
  done
fi

# ---- write .deploy-status.json --------------------------------------------
TG_STATE="${SVC_STATUS[conveyor-telegram-bot.service]:-unknown}"
FS_STATE="${SVC_STATUS[conveyor-feishu-bot.service]:-unknown}"

cat > "${STATUS_FILE}" <<STATUS_JSON
{
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "${DEPLOY_SOURCE}",
  "git_sha": "${GIT_SHA}",
  "git_ref": "${GIT_REF}",
  "run_id": "${RUN_ID}",
  "remote_dir": "${DEPLOY_PATH}",
  "smoke": "passed",
  "services": {
    "telegram": "${TG_STATE}",
    "feishu": "${FS_STATE}"
  },
  "rollback_attempted": $([ "$ALL_ACTIVE" == "false" ] && echo "true" || echo "false"),
  "previous_commit": "${OLD_COMMIT}"
}
STATUS_JSON
log "Wrote ${STATUS_FILE}"

# ---- final summary --------------------------------------------------------
log "Service status:"
for svc in "${SERVICES[@]}"; do
    log "  ${svc}: ${SVC_STATUS[$svc]:-unknown}"
done

log "Deploy complete: ${OLD_COMMIT} → ${NEW_COMMIT}"

# Exit nonzero if any service is still not active
if [[ "$ALL_ACTIVE" == "false" ]]; then
  FINAL_OK=true
  for svc in "${SERVICES[@]}"; do
    [[ "${SVC_STATUS[$svc]:-}" == "active" ]] || FINAL_OK=false
  done
  if [[ "$FINAL_OK" == "false" ]]; then
    die "Some services are not active after rollback. Manual intervention needed."
  fi
fi
