#!/usr/bin/env bash
set -euo pipefail

cd /opt/codex-telegram-runner

if [[ ! -f .env ]]; then
  echo "missing .env"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

"${PYTHON_BIN}" - <<'PY'
import os
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

required = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "CODEX_WORKSPACE_ROOT",
    "CODEX_BIN",
]
missing = [name for name in required if not os.getenv(name)]
if missing:
    raise SystemExit(f"missing required env vars: {', '.join(missing)}")
workspace = Path(os.environ["CODEX_WORKSPACE_ROOT"])
if not workspace.is_dir():
    raise SystemExit(f"workspace is not a directory: {workspace}")
print("env ok")

minimax_key = os.getenv("MINIMAX_API_KEY")
if minimax_key:
    base_url = os.getenv("MINIMAX_BASE_URL")
    model = os.getenv("CODEX_MODEL")
    config_path = Path.home() / ".codex" / "config.toml"
    if tomllib and config_path.exists():
        config = tomllib.loads(config_path.read_text())
        provider_id = config.get("model_provider")
        provider = config.get("model_providers", {}).get(provider_id, {})
        if provider_id == "minimax":
            base_url = base_url or provider.get("base_url")
            model = model or config.get("model")
            wire_api = provider.get("wire_api")
            if wire_api != "responses":
                raise SystemExit(f"minimax provider wire_api should be responses, got: {wire_api!r}")
    base_url = (base_url or "https://api.minimaxi.com/v1").rstrip("/")
    model = model or "MiniMax-M3"
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {minimax_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(500).decode("utf-8", "replace")
        raise SystemExit(f"MiniMax /models failed at {base_url}: HTTP {exc.code} {body}") from exc
    except Exception as exc:
        raise SystemExit(f"MiniMax /models failed at {base_url}: {exc}") from exc
    model_ids = {item.get("id") for item in data.get("data", []) if isinstance(item, dict)}
    if model not in model_ids:
        preview = ", ".join(sorted(x for x in model_ids if x)[:12])
        raise SystemExit(f"MiniMax model {model!r} not found at {base_url}; saw: {preview}")
    print(f"minimax models ok: {base_url} has {model}")
PY

"${CODEX_BIN}" --version
git -C "${CODEX_WORKSPACE_ROOT}" rev-parse --show-toplevel
git -C "${CODEX_WORKSPACE_ROOT}" status --short

if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  printf '%s' "${OPENAI_API_KEY}" | "${CODEX_BIN}" login --with-api-key >/dev/null
elif [[ -n "${MINIMAX_API_KEY:-}" ]]; then
  echo "using custom provider auth from MINIMAX_API_KEY"
fi

set +e
"${CODEX_BIN}" doctor | sed -n '1,120p'
doctor_status=${PIPESTATUS[0]}
set -e
if [[ ${doctor_status} -ne 0 ]]; then
  echo "codex doctor reported warnings/failures; review output above"
fi

echo "healthcheck ok"
