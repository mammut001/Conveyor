#!/usr/bin/env bash
# First-time (or full refresh) install from your laptop to a VPS.
# Syncs source, creates .venv, installs systemd units, runs configure_env.py
# when .env is missing, then healthcheck + enable services.
#
# Secrets and VPS host stay local — never commit CONVEYOR_REMOTE or .env.
#
#   CONVEYOR_REMOTE=ubuntu@<host> bash scripts/install-remote.sh
#
# After the first install, use scripts/deploy.sh for code-only updates.
set -euo pipefail

REMOTE="${CONVEYOR_REMOTE:-}"
REMOTE_DIR="${CONVEYOR_REMOTE_DIR:-/opt/conveyor}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "$REMOTE" || "$REMOTE" == *"<"* ]]; then
  echo "Set CONVEYOR_REMOTE first, e.g.:" >&2
  echo "  CONVEYOR_REMOTE=ubuntu@203.0.113.42 bash scripts/install-remote.sh" >&2
  exit 1
fi

REMOTE_USER="${REMOTE%%@*}"

EXCLUDES=(
  --exclude=.env
  --exclude=.env.*
  --exclude=.venv
  --exclude=.git
  --exclude=__pycache__
  --exclude='*.pyc'
  --exclude=logs
  --exclude=worktrees
  --exclude=snapshots
  --exclude=state
  --exclude=MEMORY.md
  --exclude='MEMORY.md.archived-*'
)

echo "==> Checking SSH to $REMOTE"
ssh "$REMOTE" 'echo ssh ok'

echo "==> Ensuring $REMOTE_DIR exists (owner: $REMOTE_USER)"
ssh "$REMOTE" "sudo mkdir -p '$REMOTE_DIR' && sudo chown -R '$REMOTE_USER:$REMOTE_USER' '$REMOTE_DIR'"

echo "==> Syncing source to $REMOTE:$REMOTE_DIR"
rsync -avz "${EXCLUDES[@]}" "$LOCAL_DIR/" "$REMOTE:$REMOTE_DIR/"

echo "==> Installing Python venv and dependencies"
ssh "$REMOTE" "bash -s" <<EOF
set -euo pipefail
cd '$REMOTE_DIR'
if ! command -v codex >/dev/null 2>&1; then
  echo 'Codex CLI not found on VPS. Install and authenticate codex first (see README).' >&2
  exit 1
fi
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt
EOF

echo "==> Installing systemd units"
ssh "$REMOTE" "bash -s" <<EOF
set -euo pipefail
cd '$REMOTE_DIR'
sudo cp systemd/conveyor-telegram-bot.service /etc/systemd/system/
sudo cp systemd/conveyor-feishu-bot.service /etc/systemd/system/
sudo cp systemd/conveyor-maintain.service /etc/systemd/system/
sudo cp systemd/conveyor-maintain.timer /etc/systemd/system/
sudo systemctl daemon-reload
EOF

if ssh "$REMOTE" "test -f '$REMOTE_DIR/.env'"; then
  echo "==> .env already exists; skipping configure_env.py"
  echo "    To reconfigure: ssh -t $REMOTE 'cd $REMOTE_DIR && .venv/bin/python scripts/configure_env.py'"
else
  echo "==> Configuring .env (interactive)"
  echo "    When prompted, send /start to your Telegram bot from the phone you want to whitelist."
  ssh -t "$REMOTE" "cd '$REMOTE_DIR' && .venv/bin/python scripts/configure_env.py"
fi

echo "==> Running healthcheck"
ssh "$REMOTE" "cd '$REMOTE_DIR' && bash scripts/healthcheck.sh"

echo "==> Enabling and starting services"
ssh "$REMOTE" "sudo systemctl enable --now conveyor-telegram-bot conveyor-feishu-bot conveyor-maintain.timer && \
  sleep 2 && sudo systemctl is-active conveyor-telegram-bot.service"

echo
echo "Install complete. Open Telegram and send /start to your bot."
echo "Code updates: CONVEYOR_REMOTE=$REMOTE bash scripts/deploy.sh"
