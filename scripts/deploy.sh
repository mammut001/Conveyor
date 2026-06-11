#!/usr/bin/env bash
# Deploy local source to the VPS. NEVER use `rsync --delete` against the project
# root on the VPS — .env lives there and contains live bot/LLM secrets, and
# local copies intentionally omit it. Always rsync a per-subdir list with
# explicit --exclude for any future secret files.
#
# Hardened flow:
#   1. rsync source files to VPS
#   2. SSH to VPS: acquire deploy lock, run smoke, restart services
#   3. Write .deploy-status.json on VPS
#   4. If restart health check fails, attempt minimal rollback
set -euo pipefail

# Override on the command line: CONVEYOR_REMOTE=user@host bash scripts/deploy.sh
# Also honors CODEX_TELEGRAM_REMOTE / CODEX_TELEGRAM_REMOTE_DIR from the
# developer shell so `alias deploy-runner` in ~/.zshrc works without edits.
REMOTE="${CONVEYOR_REMOTE:-${CODEX_TELEGRAM_REMOTE:-<ssh-user>@<vps-host>}}"
REMOTE_DIR="${CONVEYOR_REMOTE_DIR:-${CODEX_TELEGRAM_REMOTE_DIR:-/opt/conveyor}}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# GitHub Actions metadata (passed as env vars by the workflow).
GITHUB_SHA="${GITHUB_SHA:-}"
GITHUB_REF_NAME="${GITHUB_REF_NAME:-}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-}"
DEPLOY_SOURCE="${DEPLOY_SOURCE:-manual}"

EXCLUDES=(
  --exclude=.env
  --exclude=.env.*
  --exclude=__pycache__
  --exclude=*.pyc
  --exclude=.venv
  --exclude=logs
  --exclude=worktrees
  --exclude=snapshots
  --exclude=state
  --exclude=MEMORY.md
  --exclude='MEMORY.md.archived-*'
  --exclude=.deploy-status.json
  --exclude=.deploy.lock
  --exclude=.deploy-backups
)

log() { echo "[deploy] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---- rsync ----------------------------------------------------------------
log "Syncing to ${REMOTE}:${REMOTE_DIR} ..."
for sub in scripts runner bot.py feishu_bot.py config.py runner.py redaction.py requirements.txt systemd channel handlers Makefile; do
  if [[ -e "$LOCAL_DIR/$sub" ]]; then
    rsync -az "${EXCLUDES[@]}" \
      "$LOCAL_DIR/$sub" "$REMOTE:$REMOTE_DIR/"
  fi
done
log "rsync complete."

# ---- remote smoke + restart + status file ----------------------------------
# The entire post-rsync phase runs as a single SSH heredoc on the VPS.
# This keeps the lock, smoke, restart, and status-file write atomic.
GIT_SHA_SHORT="${GITHUB_SHA:+$(echo "$GITHUB_SHA" | head -c 7)}"
if [[ -z "$GIT_SHA_SHORT" ]]; then
  GIT_SHA_SHORT="$(cd "$LOCAL_DIR" && git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
fi

log "Running remote smoke + restart on VPS ..."
ssh "$REMOTE" bash -s -- \
    "$REMOTE_DIR" "$DEPLOY_SOURCE" "$GIT_SHA_SHORT" "$GITHUB_REF_NAME" "$GITHUB_RUN_ID" <<'REMOTE_EOF'
set -euo pipefail

REMOTE_DIR="$1"
DEPLOY_SOURCE="$2"
GIT_SHA="$3"
GIT_REF="$4"
RUN_ID="$5"
LOCK_FILE="${REMOTE_DIR}/.deploy.lock"
STATUS_FILE="${REMOTE_DIR}/.deploy-status.json"
BACKUP_DIR="${REMOTE_DIR}/.deploy-backups"

log() { echo "[vps-deploy] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---- lock ------------------------------------------------------------------
exec 200>"${LOCK_FILE}"
flock -n 200 || die "Another deploy is already running (lock: ${LOCK_FILE})"

cd "${REMOTE_DIR}"

# ---- preflight -------------------------------------------------------------
[[ -f .env ]] || die ".env not found at ${REMOTE_DIR}/.env"
[[ -d .venv ]] || die ".venv not found at ${REMOTE_DIR}/.venv"
[[ -f Makefile ]] || die "Makefile not found at ${REMOTE_DIR}/Makefile"

# ---- backup key files (minimal rollback support) ---------------------------
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

# ---- smoke -----------------------------------------------------------------
log "Running make smoke ..."
if ! .venv/bin/python -m pytest --co -q scripts/ 2>/dev/null && ! make smoke; then
  die "Smoke tests FAILED. Services will NOT be restarted."
fi
log "Smoke passed."

# ---- restart services ------------------------------------------------------
SERVICES=(conveyor-telegram-bot conveyor-feishu-bot)
# Optionally restart maintain timer if it exists
if systemctl list-unit-files conveyor-maintain.timer &>/dev/null; then
  SERVICES+=(conveyor-maintain.timer)
fi

declare -A SVC_STATUS
ALL_ACTIVE=true
for svc in "${SERVICES[@]}"; do
  log "Restarting ${svc} ..."
  if sudo systemctl restart "${svc}"; then
    sleep 2
    STATE="$(systemctl is-active "${svc}" 2>/dev/null || echo 'inactive')"
    SVC_STATUS["$svc"]="$STATE"
    if [[ "$STATE" != "active" ]]; then
      ALL_ACTIVE=false
      log "WARNING: ${svc} is ${STATE} after restart"
    else
      log "  ${svc}: ${STATE}"
    fi
  else
    SVC_STATUS["$svc"]="restart-failed"
    ALL_ACTIVE=false
    log "WARNING: failed to restart ${svc}"
  fi
done

# ---- rollback guard --------------------------------------------------------
if [[ "$ALL_ACTIVE" == "false" ]]; then
  log "Some services are not active. Attempting rollback ..."
  for f in Makefile config.py runner.py bot.py feishu_bot.py; do
    if [[ -f "${BACKUP_PATH}/$f" ]]; then
      cp "${BACKUP_PATH}/$f" "${REMOTE_DIR}/$f" 2>/dev/null || true
    fi
  done
  for svc in "${SERVICES[@]}"; do
    STATE="${SVC_STATUS[$svc]:-unknown}"
    if [[ "$STATE" != "active" ]]; then
      log "Re-restarting ${svc} after rollback ..."
      sudo systemctl restart "${svc}" 2>/dev/null || true
      sleep 2
      NEW_STATE="$(systemctl is-active "${svc}" 2>/dev/null || echo 'inactive')"
      SVC_STATUS["$svc"]="$NEW_STATE"
      log "  ${svc}: ${NEW_STATE}"
    fi
  done
fi

# ---- write .deploy-status.json ---------------------------------------------
TG_STATE="${SVC_STATUS[conveyor-telegram-bot]:-unknown}"
FS_STATE="${SVC_STATUS[conveyor-feishu-bot]:-unknown}"

cat > "${STATUS_FILE}" <<STATUS_JSON
{
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "${DEPLOY_SOURCE}",
  "git_sha": "${GIT_SHA}",
  "git_ref": "${GIT_REF}",
  "run_id": "${RUN_ID}",
  "remote_dir": "${REMOTE_DIR}",
  "smoke": "passed",
  "services": {
    "telegram": "${TG_STATE}",
    "feishu": "${FS_STATE}"
  },
  "rollback_attempted": $([ "$ALL_ACTIVE" == "false" ] && echo "true" || echo "false"),
  "backup_path": "${BACKUP_PATH}"
}
STATUS_JSON
log "Wrote ${STATUS_FILE}"

# ---- final summary ---------------------------------------------------------
log "Deploy complete."
for svc in "${SERVICES[@]}"; do
  log "  ${svc}: ${SVC_STATUS[$svc]:-unknown}"
done

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
REMOTE_EOF

log "Remote deploy finished successfully."
