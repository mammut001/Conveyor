#!/usr/bin/env bash
# Provision isolated runtime only. Does not enable/start services or deploy a release.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the preview host'; exit 1; }
preview_root=/srv/conveyor-preview
if ! id conveyor-preview >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin conveyor-preview
fi
install -d -m 0755 "$preview_root" "$preview_root/releases"
for preview_dir in runtime state tasks; do
  install -d -m 0700 -o conveyor-preview -g conveyor-preview "$preview_root/$preview_dir"
done
if [[ ! -f "$preview_root/runtime/preview.env" ]]; then
  python3 - <<'PY'
import os
import secrets
from pathlib import Path
path=Path('/srv/conveyor-preview/runtime/preview.env')
settings={
    'CONVEYOR_WEB_ENABLED':'1', 'CONVEYOR_WEB_HOST':'127.0.0.1',
    'CONVEYOR_WEB_PORT':'18790', 'CONVEYOR_WEB_TOKEN':secrets.token_urlsafe(48),
    'CODEX_WORKSPACE_ROOT':'/srv/conveyor-preview/tasks/workspace',
    'CODEX_TASK_ROOT':'/srv/conveyor-preview/tasks/jobs',
    'CODEX_MEMORY_ROOT':'/srv/conveyor-preview/state',
    'CODEX_BIN':'/usr/bin/false', 'CONVEYOR_AGENT_BACKEND':'codex',
    'CONVEYOR_CHAT_MODE':'off', 'CONVEYOR_ROUTINES_ENABLED':'0',
    'CONVEYOR_AGENT_DELEGATION':'0', 'CONVEYOR_AGENTS_ENABLED':'0',
    'CONVEYOR_TAKEOVER_ENABLED':'0', 'CONVEYOR_COMPUTER_USE_ENABLED':'0',
    'CONVEYOR_DESKTOP_NODE_ENABLED':'0', 'GITHUB_DEFAULT_REPO':'mammut001/Conveyor',
}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as file:
    file.write(''.join(f'{key}={value}\n' for key,value in settings.items()))
PY
  chown conveyor-preview:conveyor-preview "$preview_root/runtime/preview.env"
fi
# The coordinator owns release switching; preview can read only its own runtime.
coordinator_user="${SUDO_USER:-ubuntu}"
id "$coordinator_user" >/dev/null
usermod -a -G conveyor-preview "$coordinator_user"
chown "$coordinator_user:conveyor-preview" "$preview_root" "$preview_root/releases"
chmod 0750 "$preview_root" "$preview_root/runtime"
chmod 0755 "$preview_root/releases"
chmod 0640 "$preview_root/runtime/preview.env"
install -d -m 0700 -o "$coordinator_user" "$preview_root/coordinator"
python3 -m venv "$preview_root/venv"
echo 'Preview runtime provisioned; no production state or credentials copied.'
