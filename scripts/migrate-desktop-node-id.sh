#!/bin/bash
# Point the control plane at the VPS desktop node.
#
# Replaces CONVEYOR_DESKTOP_NODE_ID only when the current assignment is
# exactly macbook-payton. Any other value is left unchanged. A backup copy
# is written beside the env file first, then the new file is renamed into
# place. Output is the backup path and whether the id changed. Other lines,
# including secrets, are not printed.
#
# Run this on the VPS after review, then restart the control plane so it
# agrees with scripts/run-vps-computer.sh (which already exports
# CONVEYOR_DESKTOP_NODE_ID=vps-desktop). The previous node record stays in
# the desktop state file; this script does not delete history.
set -euo pipefail

env_file="${1:-/opt/conveyor/.env}"
from_id="${2:-macbook-payton}"
to_id="${3:-vps-desktop}"

if [[ ! -f "$env_file" ]]; then
  echo "env file not found: $env_file" >&2
  exit 1
fi
if [[ "$from_id" == "$to_id" || -z "$to_id" || "$to_id" == *" "* ]]; then
  echo "refusing node id update" >&2
  exit 1
fi

python3 - "$env_file" "$from_id" "$to_id" <<'PY'
import pathlib, sys, time
path, src, dst = sys.argv[1:]
file = pathlib.Path(path)
text = file.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
found = False
changed = False
out = []
for line in lines:
    body = line.split("#", 1)[0].strip()
    if body.startswith("CONVEYOR_DESKTOP_NODE_ID="):
        found = True
        value = body.split("=", 1)[1].strip().strip('"').strip("'")
        if value == dst:
            out.append(line)
            continue
        if value != src:
            print("current CONVEYOR_DESKTOP_NODE_ID is not the expected old value; file unchanged", file=sys.stderr)
            sys.exit(2)
        newline = "\n" if line.endswith("\n") else ""
        out.append(f"CONVEYOR_DESKTOP_NODE_ID={dst}{newline}")
        changed = True
        continue
    out.append(line)
if not found:
    print("CONVEYOR_DESKTOP_NODE_ID is not set; file unchanged", file=sys.stderr)
    sys.exit(2)
if not changed:
    print(f"unchanged {path}")
    sys.exit(0)
stamp = time.strftime("%Y%m%d%H%M%S")
backup = file.with_name(file.name + f".bak-desktop-node-{stamp}")
backup.write_bytes(file.read_bytes())
tmp = file.with_name(file.name + ".desktop-node.tmp")
tmp.write_text("".join(out), encoding="utf-8")
tmp.chmod(file.stat().st_mode)
tmp.replace(file)
print(f"updated {path}")
print(f"backup {backup}")
PY
