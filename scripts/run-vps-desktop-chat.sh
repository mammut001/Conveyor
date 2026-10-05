#!/bin/bash
# Open the Conveyor chat window on the live XFCE session.
# Display variables come from xfce4-session, so an xrdp reconnect
# is picked up the next time this process starts.
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

cd /opt/conveyor
exec /opt/conveyor/.venv/bin/python /opt/conveyor/desktop_chat_window.py
