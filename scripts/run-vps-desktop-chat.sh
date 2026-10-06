#!/bin/bash
# Open the Conveyor chat window on the live XFCE session.
# Display variables come from the live desktop session, so a restarted
# desktop is picked up the next time this process starts.
set -euo pipefail

session_env="$(python3 "$(dirname "$(readlink -f "$0")")/desktop_session_env.py")"
eval "$session_env"

cd /opt/conveyor
exec /opt/conveyor/.venv/bin/python /opt/conveyor/desktop_chat_window.py
