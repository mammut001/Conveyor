#!/usr/bin/env bash
# deploy_vps.sh — transactional git-based production deploy.
#
# The target revision is validated in a detached candidate worktree before the
# live checkout moves. Production cutover is guarded by an idle queue check,
# an online SQLite backup, a clean tracked checkout, post-cutover smoke tests,
# and whole-revision rollback if dependency sync, smoke, or service health fails.
set -euo pipefail

DEPLOY_PATH="${CONVEYOR_DEPLOY_PATH:-/opt/conveyor}"
LOCK_FILE="${DEPLOY_PATH}/.deploy.lock"
STATUS_FILE="${DEPLOY_PATH}/.deploy-status.json"
BACKUP_DIR="${DEPLOY_PATH}/.deploy-backups"
LOG_PREFIX="[deploy]"
DEPLOY_SOURCE="${DEPLOY_SOURCE:-manual}"
REQUESTED_SHA="${GITHUB_SHA:-}"
GIT_REF="${GITHUB_REF_NAME:-}"
RUN_ID="${GITHUB_RUN_ID:-}"
CANDIDATE=""
ROLLBACK_ATTEMPTED=false

log() { echo "${LOG_PREFIX} $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

clean_candidate() {
  if [[ -n "${CANDIDATE}" ]]; then
    git -C "${DEPLOY_PATH}" worktree remove --force "${CANDIDATE}" >/dev/null 2>&1 || rm -rf "${CANDIDATE}" || true
  fi
}
trap clean_candidate EXIT

clean_live_checkout() {
  git clean -fd \
    --exclude=.env \
    --exclude=.venv \
    --exclude=.deploy-status.json \
    --exclude=.deploy.lock \
    --exclude=.deploy-backups \
    --quiet
}

# ---- lock -----------------------------------------------------------------
exec 200>"${LOCK_FILE}"
flock -n 200 || die "Another deploy is already running (lock: ${LOCK_FILE})"

# ---- preflight ------------------------------------------------------------
cd "${DEPLOY_PATH}"
[[ -f .env ]] || die ".env not found at ${DEPLOY_PATH}/.env"
[[ -x .venv/bin/python ]] || die ".venv/bin/python not found"
[[ -f scripts/deploy_vps.sh ]] || die "scripts/deploy_vps.sh not found"

TRACKED_DIRTY="$(git status --porcelain=v1 --untracked-files=no)"
[[ -z "${TRACKED_DIRTY}" ]] || die "Tracked production files are modified; refusing to overwrite them"

OLD_COMMIT_FULL="$(git rev-parse HEAD)"
OLD_COMMIT="$(git rev-parse --short HEAD)"
log "Current commit: ${OLD_COMMIT}"

log "Fetching origin/main..."
git fetch origin main --quiet
ORIGIN_MAIN="$(git rev-parse origin/main)"
if [[ -n "${REQUESTED_SHA}" ]]; then
  git cat-file -e "${REQUESTED_SHA}^{commit}" 2>/dev/null || die "Requested SHA is not available after fetching origin/main"
  TARGET_COMMIT_FULL="$(git rev-parse "${REQUESTED_SHA}^{commit}")"
  git merge-base --is-ancestor "${TARGET_COMMIT_FULL}" "${ORIGIN_MAIN}" \
    || die "Requested SHA is not reachable from current origin/main"
else
  TARGET_COMMIT_FULL="${ORIGIN_MAIN}"
fi
TARGET_COMMIT="$(git rev-parse --short "${TARGET_COMMIT_FULL}")"
log "Target commit:  ${TARGET_COMMIT}"

# ---- locate shared control-plane database and require idle queue -----------
DB_PATH="$(.venv/bin/python - <<'PY'
from pathlib import Path
from dotenv import dotenv_values
values = dotenv_values('.env')
root = Path(values.get('CODEX_MEMORY_ROOT') or '~/.codex').expanduser().resolve()
print(root / 'state' / 'job_queue.sqlite3')
PY
)"
read -r QUEUED_COUNT RUNNING_COUNT < <(.venv/bin/python - "${DB_PATH}" <<'PY'
import sqlite3, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    print('0 0')
    raise SystemExit
conn = sqlite3.connect(str(path), timeout=10)
try:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queued_jobs'").fetchone()
    if not exists:
        print('0 0')
    else:
        queued = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state='queued'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state='running'").fetchone()[0]
        print(f'{queued} {running}')
finally:
    conn.close()
PY
)
[[ "${QUEUED_COUNT}" == "0" && "${RUNNING_COUNT}" == "0" ]] \
  || die "Queue is not idle (queued=${QUEUED_COUNT}, running=${RUNNING_COUNT}); retry after jobs finish"
log "Queue idle: queued=0 running=0"

# ---- backup current release metadata, secrets file, and SQLite ------------
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_PATH="${BACKUP_DIR}/${TIMESTAMP}"
mkdir -p "${BACKUP_PATH}"
chmod 700 "${BACKUP_PATH}" 2>/dev/null || true
printf '%s\n' "${OLD_COMMIT_FULL}" > "${BACKUP_PATH}/previous-commit.txt"
cp -p .env "${BACKUP_PATH}/conveyor.env"
chmod 600 "${BACKUP_PATH}/conveyor.env" 2>/dev/null || true
[[ -f requirements.txt ]] && cp requirements.txt "${BACKUP_PATH}/requirements.txt"
git status --porcelain=v1 > "${BACKUP_PATH}/git-status.txt"

if [[ -f "${DB_PATH}" ]]; then
  .venv/bin/python - "${DB_PATH}" "${BACKUP_PATH}/job_queue.sqlite3" <<'PY'
import sqlite3, sys
source, target = sys.argv[1:3]
src = sqlite3.connect(source, timeout=10)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
    result = dst.execute('PRAGMA integrity_check').fetchone()[0]
    if result != 'ok':
        raise SystemExit(f'backup integrity_check failed: {result}')
finally:
    dst.close(); src.close()
PY
fi
log "Backup complete: ${BACKUP_PATH}"

# Keep only the five newest release backups after a successful new backup.
ls -1dt "${BACKUP_DIR}"/*/ 2>/dev/null | tail -n +6 | xargs rm -rf 2>/dev/null || true

# ---- candidate validation before touching live source ---------------------
CANDIDATE="$(mktemp -d /tmp/conveyor-deploy-candidate.XXXXXX)"
rmdir "${CANDIDATE}"
git worktree add --detach "${CANDIDATE}" "${TARGET_COMMIT_FULL}" --quiet
log "Validating detached candidate ${TARGET_COMMIT}..."
python3 -m venv "${CANDIDATE}/.venv"
PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 \
  "${CANDIDATE}/.venv/bin/python" -m pip install -q -r "${CANDIDATE}/requirements.txt"
(
  cd "${CANDIDATE}"
  .venv/bin/python -m compileall -q .
  if ! make smoke; then
    exit 1
  fi
) || die "Candidate validation FAILED; live checkout was not changed"
log "Candidate validation passed."
clean_candidate
CANDIDATE=""

# Capture only services that are currently active; deployment must not enable
# or revive unrelated/pre-existing disabled services.
ALL_CANDIDATE_SERVICES=(
  conveyor-telegram-bot.service
  conveyor-feishu-bot.service
  conveyor-desktop-agent.service
  conveyor-web.service
  conveyor-maintain.timer
)
SERVICES=()
for svc in "${ALL_CANDIDATE_SERVICES[@]}"; do
  if sudo -n systemctl is-active --quiet "${svc}" 2>/dev/null; then
    SERVICES+=("${svc}")
  fi
done

declare -A SVC_STATUS

rollback_release() {
  local reason="$1"
  ROLLBACK_ATTEMPTED=true
  log "Cutover failed: ${reason}. Rolling back whole source revision to ${OLD_COMMIT}..."
  git reset --hard "${OLD_COMMIT_FULL}" --quiet || true
  clean_live_checkout || true
  if [[ -f requirements.txt ]]; then
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 .venv/bin/python -m pip install -q -r requirements.txt || true
  fi
  local rollback_ok=true
  for svc in "${SERVICES[@]}"; do
    sudo -n systemctl restart "${svc}" >/dev/null 2>&1 || rollback_ok=false
    sleep 2
    state="$(sudo -n systemctl is-active "${svc}" 2>/dev/null || echo inactive)"
    SVC_STATUS["${svc}"]="${state}"
    [[ "${state}" == "active" ]] || rollback_ok=false
  done
  if [[ "${rollback_ok}" == "true" ]]; then
    log "Rollback restored ${OLD_COMMIT}; database backup remains at ${BACKUP_PATH}."
  else
    log "ERROR: rollback source restored but one or more services are unhealthy; manual intervention required." >&2
  fi
  return 1
}

# ---- live cutover ---------------------------------------------------------
if [[ "${OLD_COMMIT_FULL}" != "${TARGET_COMMIT_FULL}" ]]; then
  log "Cutting over live checkout to ${TARGET_COMMIT}..."
  git reset --hard "${TARGET_COMMIT_FULL}" --quiet || rollback_release "git reset"
  clean_live_checkout || rollback_release "git clean"
else
  log "Live checkout already has target revision; validating/restarting it."
fi

log "Syncing production Python dependencies..."
PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 .venv/bin/python -m pip install -q -r requirements.txt \
  || rollback_release "dependency sync"

log "Running production smoke tests..."
if ! make smoke; then
  rollback_release "production smoke"
fi
log "Production smoke passed."

# ---- restart previously-active services and health-check ------------------
ALL_ACTIVE=true
for svc in "${SERVICES[@]}"; do
  log "Restarting ${svc}..."
  if ! sudo -n systemctl restart "${svc}"; then
    ALL_ACTIVE=false
  fi
  sleep 2
  STATE="$(sudo -n systemctl is-active "${svc}" 2>/dev/null || echo inactive)"
  SVC_STATUS["${svc}"]="${STATE}"
  if [[ "${STATE}" != "active" ]]; then
    ALL_ACTIVE=false
    log "WARNING: ${svc} is ${STATE} after restart"
  else
    log "  ${svc}: ${STATE}"
  fi
done

if [[ "${ALL_ACTIVE}" != "true" ]]; then
  rollback_release "service health check" || die "Deployment rolled back after service health failure"
fi

NEW_COMMIT_FULL="$(git rev-parse HEAD)"
NEW_COMMIT="$(git rev-parse --short HEAD)"
[[ "${NEW_COMMIT_FULL}" == "${TARGET_COMMIT_FULL}" ]] || rollback_release "post-cutover SHA mismatch"

# ---- write deployment status ---------------------------------------------
TG_STATE="${SVC_STATUS[conveyor-telegram-bot.service]:-inactive-before-deploy}"
FS_STATE="${SVC_STATUS[conveyor-feishu-bot.service]:-inactive-before-deploy}"
DESKTOP_STATE="${SVC_STATUS[conveyor-desktop-agent.service]:-inactive-before-deploy}"
WEB_STATE="${SVC_STATUS[conveyor-web.service]:-inactive-before-deploy}"

cat > "${STATUS_FILE}" <<STATUS_JSON
{
  "deployed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "${DEPLOY_SOURCE}",
  "git_sha": "${NEW_COMMIT_FULL}",
  "git_ref": "${GIT_REF}",
  "run_id": "${RUN_ID}",
  "remote_dir": "${DEPLOY_PATH}",
  "smoke": "passed",
  "backup_path": "${BACKUP_PATH}",
  "database_path": "${DB_PATH}",
  "services": {
    "telegram": "${TG_STATE}",
    "feishu": "${FS_STATE}",
    "desktop_agent_server": "${DESKTOP_STATE}",
    "web": "${WEB_STATE}"
  },
  "rollback_attempted": ${ROLLBACK_ATTEMPTED},
  "previous_commit": "${OLD_COMMIT_FULL}"
}
STATUS_JSON
log "Wrote ${STATUS_FILE}"

log "Service status:"
for svc in "${SERVICES[@]}"; do
  log "  ${svc}: ${SVC_STATUS[$svc]:-unknown}"
done
log "Deploy complete: ${OLD_COMMIT} → ${NEW_COMMIT}"
