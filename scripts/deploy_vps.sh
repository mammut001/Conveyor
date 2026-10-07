#!/usr/bin/env bash
# deploy_vps.sh — transactional git-based production deploy.
#
# The target revision is validated in a detached candidate worktree before the
# live checkout moves. Production cutover is guarded by an idle queue check,
# an online SQLite backup, a clean tracked checkout, post-cutover smoke tests,
# and whole-revision rollback if dependency sync, smoke, or service health fails.
#
# When the deploy user differs from the user the services run as, the queue
# database (service user's ~/.codex, mode 700) is read through scripts/deploy_db.py
# as that user, which needs one sudoers rule, e.g.:
#   deploy ALL=(ubuntu) NOPASSWD: /opt/conveyor/.venv/bin/python /opt/conveyor/scripts/deploy_db.py *
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
LOCAL_BUNDLE="${CONVEYOR_DEPLOY_BUNDLE:-}"
SERVICES_STOPPED=false
DEPLOY_SUCCEEDED=false
CANDIDATE=""
ROLLBACK_ATTEMPTED=false

log() { echo "${LOG_PREFIX} $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

clean_candidate() {
  if [[ -n "${CANDIDATE}" ]]; then
    git -C "${DEPLOY_PATH}" worktree remove --force "${CANDIDATE}" >/dev/null 2>&1 || rm -rf "${CANDIDATE}" || true
  fi
}
finish_deploy() {
  local result=$?
  clean_candidate
  if [[ "${SERVICES_STOPPED}" == "true" && "${DEPLOY_SUCCEEDED}" != "true" && "${ROLLBACK_ATTEMPTED}" != "true" ]]; then
    rollback_release "unexpected deployment exit" || true
  fi
  return "${result}"
}
trap finish_deploy EXIT

clean_live_checkout() {
  git clean -fd \
    --exclude=.env \
    --exclude=.venv \
    --exclude=.deploy-status.json \
    --exclude=.deploy.lock \
    --exclude=.deploy-backups \
    --quiet
}

write_smoke_fixture() {
  local root="$1"
  cat > "${root}/.env.test" <<EOF
TELEGRAM_BOT_TOKEN=deploy-placeholder-token
TELEGRAM_ALLOWED_USER_ID=1
CODEX_WORKSPACE_ROOT=${root}
CODEX_TASK_ROOT=${root}/.smoke-task
CODEX_MEMORY_ROOT=${root}/.smoke-memory
USER_TIMEZONE=UTC
CONVEYOR_DESKTOP_AGENT_TOKEN=deploy-desktop-placeholder-token
EOF
  chmod 600 "${root}/.env.test"
}

clean_smoke_fixture() {
  local root="$1"
  rm -f "${root}/.env.test"
  rm -rf "${root}/.smoke-task" "${root}/.smoke-memory"
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

if [[ -n "${LOCAL_BUNDLE}" ]]; then
  # An authenticated SSH operator can deploy a committed local revision
  # without publishing it to GitHub. It must extend the current release.
  [[ "${REQUESTED_SHA}" =~ ^[a-f0-9]{40}$ ]] || die "Local bundle deployment requires an exact GITHUB_SHA"
  [[ -f "${LOCAL_BUNDLE}" ]] || die "Local Git bundle not found"
  git bundle verify "${LOCAL_BUNDLE}" >/dev/null 2>&1 || die "Invalid local Git bundle"
  git fetch --no-tags "${LOCAL_BUNDLE}" HEAD --quiet || die "Cannot import local Git bundle"
  TARGET_COMMIT_FULL="$(git rev-parse FETCH_HEAD)"
  [[ "${TARGET_COMMIT_FULL}" == "${REQUESTED_SHA}" ]] || die "Bundle HEAD does not match requested SHA"
  git merge-base --is-ancestor "${OLD_COMMIT_FULL}" "${TARGET_COMMIT_FULL}" \
    || die "Local bundle must extend the current production revision"
else
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
fi
TARGET_COMMIT="$(git rev-parse --short "${TARGET_COMMIT_FULL}")"
log "Target commit:  ${TARGET_COMMIT}"

# ---- locate shared control-plane database and require idle queue -----------
# The database belongs to the user the services run as, which is usually not
# the deploy user: resolving ~/.codex here would inspect the wrong (stale)
# file, so the helper runs as the service user.
SERVICE_USER=""
for unit in conveyor-telegram-bot.service conveyor-feishu-bot.service conveyor-web.service; do
  SERVICE_USER="$(systemctl show -p User --value "${unit}" 2>/dev/null || true)"
  [[ -n "${SERVICE_USER}" ]] && break
done
[[ -n "${SERVICE_USER}" ]] || SERVICE_USER="$(id -un)"

deploy_db() {
  local helper="${DEPLOY_PATH}/scripts/deploy_db.py"
  if [[ "${SERVICE_USER}" == "$(id -un)" ]]; then
    "${DEPLOY_PATH}/.venv/bin/python" "${helper}" "$@"
  else
    sudo -n -u "${SERVICE_USER}" "${DEPLOY_PATH}/.venv/bin/python" "${helper}" "$@"
  fi
}

[[ -f "${DEPLOY_PATH}/scripts/deploy_db.py" ]] \
  || die "scripts/deploy_db.py is missing from the live checkout; cannot verify the queue"
if ! DB_PATH="$(deploy_db path)"; then
  die "Cannot inspect the queue database as ${SERVICE_USER}. Add to sudoers: $(id -un) ALL=(${SERVICE_USER}) NOPASSWD: ${DEPLOY_PATH}/.venv/bin/python ${DEPLOY_PATH}/scripts/deploy_db.py *"
fi
read -r QUEUED_COUNT RUNNING_COUNT < <(deploy_db idle) \
  || die "Could not read queue state from ${DB_PATH}"
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

deploy_db backup > "${BACKUP_PATH}/job_queue.sqlite3" \
  || die "Queue database backup failed (${DB_PATH})"
if [[ -s "${BACKUP_PATH}/job_queue.sqlite3" ]]; then
  chmod 600 "${BACKUP_PATH}/job_queue.sqlite3" 2>/dev/null || true
else
  rm -f "${BACKUP_PATH}/job_queue.sqlite3"
  log "No queue database at ${DB_PATH}; nothing to back up"
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

# Candidate smoke uses isolated placeholders only; it never needs production secrets.
write_smoke_fixture "${CANDIDATE}"
(
  cd "${CANDIDATE}"
  .venv/bin/python -m compileall -q .
  CONVEYOR_ENV_FILE=.env.test .venv/bin/python -m unittest discover -s tests -q
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
  conveyor-vps-computer.service
  conveyor-desktop-chat.service
  conveyor-handoff.service
  conveyor-agent-desktops.service
  conveyor-maintain.timer
  conveyor-scheduler.timer
)
SERVICES=()
for svc in "${ALL_CANDIDATE_SERVICES[@]}"; do
  if sudo -n systemctl is-active --quiet "${svc}" 2>/dev/null; then
    SERVICES+=("${svc}")
  else
    log "Skipping ${svc} (not active at capture)"
  fi
done

declare -A SVC_STATUS

rollback_release() {
  local reason="$1"
  ROLLBACK_ATTEMPTED=true
  clean_smoke_fixture "${DEPLOY_PATH}" || true
  log "Cutover failed: ${reason}. Rolling back whole source revision to ${OLD_COMMIT}..."
  git reset --hard "${OLD_COMMIT_FULL}" --quiet || true
  clean_live_checkout || true
  if [[ -f requirements.txt ]]; then
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 .venv/bin/python -m pip install -q -r requirements.txt || true
  fi
  local rollback_ok=true
  local state
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

# Quiesce request producers only after candidate validation. Then re-read
# authoritative queue state: work may have arrived while tests were running.
SERVICES_STOPPED=true
for svc in "${SERVICES[@]}"; do
  sudo -n systemctl stop "${svc}" || die "Could not stop ${svc} before cutover"
done
read -r QUEUED_COUNT RUNNING_COUNT < <(deploy_db idle) \
  || die "Could not recheck queue after stopping services"
[[ "${QUEUED_COUNT}" == "0" && "${RUNNING_COUNT}" == "0" ]] \
  || die "Queue changed during validation (queued=${QUEUED_COUNT}, running=${RUNNING_COUNT}); restoring services without deploying"

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

log "Running production smoke tests with isolated non-secret fixture..."
write_smoke_fixture "${DEPLOY_PATH}"
if ! make smoke; then
  clean_smoke_fixture "${DEPLOY_PATH}"
  rollback_release "production smoke"
fi
clean_smoke_fixture "${DEPLOY_PATH}"
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

# ---- report systemd unit drift ----------------------------------------------
# Deploys never install unit files (that needs root), so an edited unit in the
# repo silently stays unapplied. Say so instead of letting it rot.
UNIT_DRIFT=()
for unit in systemd/*.service systemd/*.timer; do
  installed="/etc/systemd/system/$(basename "${unit}")"
  [[ -f "${installed}" ]] || continue
  cmp -s "${unit}" "${installed}" || UNIT_DRIFT+=("$(basename "${unit}")")
done
if (( ${#UNIT_DRIFT[@]} )); then
  log "WARNING: installed systemd units differ from the repo: ${UNIT_DRIFT[*]}"
  log "         apply with: sudo install -m 0644 ${DEPLOY_PATH}/systemd/<unit> /etc/systemd/system/ && sudo systemctl daemon-reload"
fi

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

DEPLOY_SUCCEEDED=true
