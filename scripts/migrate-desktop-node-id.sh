#!/bin/bash
# Point the control plane at the VPS desktop node.
#
# One optional, explicit migration. It rewrites CONVEYOR_DESKTOP_NODE_ID and
# CONVEYOR_DESKTOP_NODE_NAME in the same transaction when the current id is
# exactly macbook-payton (or already vps-desktop) and the current name is
# exactly "Payton MacBook". Any other id is left unchanged, including its
# name, so a real Mac observer is not renamed. Duplicate assignments are
# rejected. The previous node record stays in the desktop state file; this
# script does not delete history.
#
# The backup is created mode 0600 with O_EXCL before any rewrite. The temp
# file is opened 0600 before its contents are written, then receives the
# original mode and ownership. Output is the backup path and whether the
# file changed. Other lines, including secrets, are not printed.
#
# Run this on the VPS after review, then restart the control plane so it
# agrees with scripts/run-vps-computer.sh. That script uses vps-desktop /
# "VPS desktop" only when the id and name are absent from the environment
# and from this file. It does not override a configured id on its own.
set -euo pipefail

env_file="${1:-/opt/conveyor/.env}"
from_id="${2:-macbook-payton}"
to_id="${3:-vps-desktop}"
from_name="${4:-Payton MacBook}"
to_name="${5:-VPS desktop}"

if [[ ! -f "$env_file" ]]; then
  echo "env file not found: $env_file" >&2
  exit 1
fi

python3 - "$env_file" "$from_id" "$to_id" "$from_name" "$to_name" <<'PY'
import os
import pathlib
import stat
import sys
import time

path, src, dst, src_name, dst_name = sys.argv[1:]
file = pathlib.Path(path)
node_id_re = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


if not node_id_re.fullmatch(src) or not node_id_re.fullmatch(dst) or src == dst:
    fail("refusing node id update", 1)
if not src_name or not dst_name or "\n" in src_name or "\n" in dst_name or "\r" in src_name or "\r" in dst_name:
    fail("refusing node name update", 1)

text = file.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)


def assignment(line: str, key: str):
    body = line.split("#", 1)[0].strip()
    prefix = "export "
    if body.startswith(prefix):
        body = body[len(prefix):].strip()
    token = key + "="
    if not body.startswith(token):
        return None
    value = body[len(token):].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


id_hits = [assignment(line, "CONVEYOR_DESKTOP_NODE_ID") for line in lines]
name_hits = [assignment(line, "CONVEYOR_DESKTOP_NODE_NAME") for line in lines]
id_values = [value for value in id_hits if value is not None]
name_values = [value for value in name_hits if value is not None]
if len(id_values) > 1 or len(name_values) > 1:
    fail("duplicate desktop node assignment; file unchanged")
if len(id_values) != 1:
    fail("CONVEYOR_DESKTOP_NODE_ID is not set once; file unchanged")

current_id = id_values[0]
current_name = name_values[0] if name_values else None
if not node_id_re.fullmatch(current_id):
    fail("current CONVEYOR_DESKTOP_NODE_ID is not a valid node id; file unchanged")
if current_id not in (src, dst):
    fail("current CONVEYOR_DESKTOP_NODE_ID is not the expected old value; file unchanged")

replace_name = current_name == src_name and current_name != dst_name
replace_id = current_id == src
if not replace_id and not replace_name:
    print(f"unchanged {path}")
    sys.exit(0)

out = []
for line in lines:
    id_value = assignment(line, "CONVEYOR_DESKTOP_NODE_ID")
    name_value = assignment(line, "CONVEYOR_DESKTOP_NODE_NAME")
    newline = "\n" if line.endswith("\n") else ""
    if replace_id and id_value is not None:
        out.append(f"CONVEYOR_DESKTOP_NODE_ID={dst}{newline}")
        continue
    if replace_name and name_value is not None:
        out.append(f"CONVEYOR_DESKTOP_NODE_NAME={dst_name}{newline}")
        continue
    out.append(line)

stamp = time.strftime("%Y%m%d%H%M%S")
backup = file.with_name(file.name + f".bak-desktop-node-{stamp}")
tmp = file.with_name(file.name + ".desktop-node.tmp")
original = file.stat()
payload = "".join(out).encode("utf-8")


def write_exclusive(target: pathlib.Path, data: bytes) -> None:
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    except Exception:
        os.close(fd)
        target.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)


try:
    write_exclusive(backup, file.read_bytes())
    write_exclusive(tmp, payload)
    os.chmod(tmp, stat.S_IMODE(original.st_mode))
    try:
        os.chown(tmp, original.st_uid, original.st_gid)
    except PermissionError:
        pass
    os.replace(tmp, file)
except Exception:
    tmp.unlink(missing_ok=True)
    raise

print(f"updated {path}")
print(f"backup {backup}")
PY
