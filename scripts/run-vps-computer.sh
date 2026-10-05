#!/bin/bash
# Claim Conveyor computer-use steps and run them on this VPS desktop.
# Display variables come from the live XFCE session, so an xrdp reconnect
# is picked up on the next start.
set -euo pipefail

eval "$(python3 - << 'PY'
import shlex
import subprocess
from pathlib import Path

wanted = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XAUTHORITY")
pid = None
for line in subprocess.check_output(["ps", "-eo", "pid,cmd"], text=True).splitlines():
    if "xfce4-session" in line and "awk" not in line:
        pid = line.split(None, 1)[0]
        break
if not pid:
    raise SystemExit("no xfce session")
found = {}
raw = Path("/proc/" + pid + "/environ").read_bytes().split(b"\0")
for item in raw:
    if b"=" not in item:
        continue
    key, val = item.split(b"=", 1)
    name = key.decode()
    if name in wanted:
        found[name] = val.decode("utf-8", "replace")
found.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))
for name, val in found.items():
    print("export " + name + "=" + shlex.quote(val))
PY
)"

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
exec /opt/conveyor/.venv/bin/python /opt/conveyor/desktop_agent.py --poll-computer
