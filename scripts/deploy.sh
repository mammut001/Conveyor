#!/usr/bin/env bash
# Manual production deploy entrypoint.
#
# This intentionally does NOT rsync the developer working tree into production.
# Manual deploys use the same canonical, transactional origin/main path as CI:
# fetch origin/main locally, stream that revision's deploy_vps.sh over SSH, and
# ask the remote script to deploy the exact main commit.
set -euo pipefail

REMOTE="${CONVEYOR_REMOTE:-${CODEX_TELEGRAM_REMOTE:-<ssh-user>@<vps-host>}}"
REMOTE_DIR="${CONVEYOR_REMOTE_DIR:-${CODEX_TELEGRAM_REMOTE_DIR:-/opt/conveyor}}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_SOURCE="${DEPLOY_SOURCE:-manual}"

log() { echo "[deploy] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ "${REMOTE}" != "<ssh-user>@<vps-host>" ]] || die "Set CONVEYOR_REMOTE (or CODEX_TELEGRAM_REMOTE) first"

cd "${LOCAL_DIR}"
log "Fetching canonical origin/main..."
git fetch origin main --quiet
TARGET_COMMIT="$(git rev-parse origin/main)"
git cat-file -e "${TARGET_COMMIT}:scripts/deploy_vps.sh" 2>/dev/null \
  || die "origin/main does not contain scripts/deploy_vps.sh"

# Stream the deployer from the exact target revision. This prevents a manual
# rollout from executing either an unmerged local deploy script or the stale
# deploy script currently installed on the VPS.
printf -v REMOTE_COMMAND \
  'GITHUB_SHA=%q GITHUB_REF_NAME=%q GITHUB_RUN_ID=%q DEPLOY_SOURCE=%q CONVEYOR_DEPLOY_PATH=%q bash -s' \
  "${TARGET_COMMIT}" "main" "manual" "${DEPLOY_SOURCE}" "${REMOTE_DIR}"

log "Deploying origin/main ${TARGET_COMMIT:0:12} to ${REMOTE}:${REMOTE_DIR}..."
git show "${TARGET_COMMIT}:scripts/deploy_vps.sh" \
  | ssh "${REMOTE}" "${REMOTE_COMMAND}"
log "Transactional production deploy finished."
