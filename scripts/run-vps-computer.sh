#!/bin/bash
# Claim Conveyor computer-use steps and run them on this VPS desktop.
# Display variables come from the live desktop session, so a restarted
# desktop is picked up on the next start.
set -euo pipefail

session_env="$(python3 "$(dirname "$(readlink -f "$0")")/desktop_session_env.py")"
eval "$session_env"

export PATH="$HOME/.local/bin:${PATH}"
export LD_LIBRARY_PATH="$HOME/.local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CONVEYOR_CONTROL_PLANE_URL="${CONVEYOR_CONTROL_PLANE_URL:-http://127.0.0.1:8766}"

socket="${HOME}/.cache/cua-driver/cua-driver.sock"
mkdir -p "$(dirname "$socket")"
if ! cua-driver status >/dev/null 2>&1; then
  rm -f "$socket"
  cua-driver serve --socket "$socket" >"${HOME}/.cache/cua-driver/serve.log" 2>&1 &
  for _ in $(seq 1 40); do
    cua-driver status >/dev/null 2>&1 && break
    sleep 0.25
  done
fi
if ! cua-driver status >/dev/null 2>&1; then
  echo "cua-driver serve did not become ready" >&2
  exit 1
fi

cd /opt/conveyor
# This process is the shared VPS desktop, not the retired Mac node.
# load_dotenv() does not override variables already set here, so the
# control plane must use the same id. Apply it with
# scripts/migrate-desktop-node-id.sh (exact macbook-payton -> vps-desktop)
# and restart the control plane. The old node record is left in place
# and stops looking online once this process no longer heartbeats it.
export CONVEYOR_DESKTOP_NODE_ID=vps-desktop
exec /opt/conveyor/.venv/bin/python /opt/conveyor/desktop_agent.py --poll-computer
