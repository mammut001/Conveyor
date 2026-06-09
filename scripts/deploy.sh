#!/usr/bin/env bash
# Deploy local source to the VPS. NEVER use `rsync --delete` against the project
# root on the VPS — .env lives there and contains live bot/LLM secrets, and
# local copies intentionally omit it. Always rsync a per-subdir list with
# explicit --exclude for any future secret files.
set -euo pipefail

# Override on the command line: CODEX_TELEGRAM_REMOTE=user@host bash scripts/deploy.sh
# First install from laptop: bash scripts/install-remote.sh (same env vars)
REMOTE="${CODEX_TELEGRAM_REMOTE:-<ssh-user>@<vps-host>}"
REMOTE_DIR="${CODEX_TELEGRAM_REMOTE_DIR:-/opt/codex-telegram-runner}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

EXCLUDES=(
  --exclude=.env
  --exclude=.env.*
  --exclude=__pycache__
  --exclude=*.pyc
  --exclude=.venv
)

for sub in scripts runner bot.py config.py runner.py redaction.py requirements.txt systemd; do
  rsync -avz "${EXCLUDES[@]}" \
    "$LOCAL_DIR/$sub" "$REMOTE:$REMOTE_DIR/"
done

ssh "$REMOTE" "rm -rf $REMOTE_DIR/scripts/__pycache__ $REMOTE_DIR/__pycache__ $REMOTE_DIR/runner/__pycache__ 2>/dev/null; \
  sudo systemctl restart codex-telegram-bot.service; \
  sleep 2; \
  sudo systemctl is-active codex-telegram-bot.service"
