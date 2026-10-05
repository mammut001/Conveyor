from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from config import Settings
from scripts.job_metadata import job_sort_time


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        status = "ok" if self.ok else "fail"
        return f"[{status}] {self.name}: {self.detail}"


def run_command(args: list[str], cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def check_systemd_active(service_name: str) -> CheckResult:
    if not shutil.which("systemctl"):
        return CheckResult("systemd", False, "systemctl is not available")
    result = run_command(["systemctl", "is-active", service_name], timeout=10)
    state = (result.stdout or result.stderr).strip()
    return CheckResult("systemd", result.returncode == 0 and state == "active", f"{service_name} is {state or 'unknown'}")


def _codex_provider_config() -> tuple[str, dict, str | None]:
    """Return (provider_id, provider_table, model) from the Codex CLI config."""
    # provider_config's parser instead of tomllib: the VPS runs Python 3.10.
    from provider_config import _parse_simple_config

    config_path = Path.home() / ".codex" / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", {}, None
    top, providers = _parse_simple_config(text)
    provider_id = top.get("model_provider", "")
    return provider_id, providers.get(provider_id, {}), top.get("model") or None


def check_provider_models(settings: Settings) -> CheckResult:
    """Probe the `/models` endpoint of whichever provider Codex is configured for."""
    import os

    provider_id, provider, config_model = _codex_provider_config()
    if not provider_id:
        if os.getenv("MINIMAX_API_KEY"):
            # Legacy deployments export MINIMAX_* without a config.toml provider.
            provider_id = "minimax"
            provider = {"env_key": "MINIMAX_API_KEY", "base_url": "https://api.minimaxi.com/v1"}
            config_model = config_model or "MiniMax-M3"
        else:
            return CheckResult("provider", True, "codex default provider (no API key probe)")

    env_key = str(provider.get("env_key") or "OPENAI_API_KEY")
    key = os.getenv(env_key)
    if not key:
        return CheckResult("provider", False, f"{env_key} is not set (provider={provider_id})")

    base_url = provider.get("base_url") or ""
    if provider_id == "minimax":
        base_url = os.getenv("MINIMAX_BASE_URL") or base_url
    base_url = str(base_url).rstrip("/")
    if not base_url:
        return CheckResult("provider", False, f"provider {provider_id} has no base_url in config.toml")
    model = settings.codex_model or config_model
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        return CheckResult("provider", False, f"{base_url}/models HTTP {exc.code}: {body}")
    except Exception as exc:
        return CheckResult("provider", False, f"{base_url}/models failed: {exc}")

    model_ids = {item.get("id") for item in data.get("data", []) if isinstance(item, dict)}
    if model and model not in model_ids:
        preview = ", ".join(sorted(x for x in model_ids if x)[:12])
        return CheckResult("provider", False, f"{model} not listed at {base_url}; saw: {preview}")
    return CheckResult("provider", True, f"{provider_id}: {base_url} lists {model or 'models'}")


def latest_job_dir(settings: Settings) -> Path | None:
    logs_root = settings.codex_task_root / "logs"
    if not logs_root.exists():
        return None
    candidates = [path for path in logs_root.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=job_sort_time)


def latest_attempt_file(job_dir: Path) -> Path | None:
    attempts = sorted(job_dir.glob("attempt-*.jsonl"), key=lambda path: path.stat().st_mtime)
    return attempts[-1] if attempts else None


def latest_final_file(job_dir: Path) -> Path | None:
    finals = sorted(job_dir.glob("attempt-*-final.txt"), key=lambda path: path.stat().st_mtime)
    return finals[-1] if finals else None


def attempt_completed(attempt_file: Path) -> bool:
    for line in attempt_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            return True
    return False


def print_results(results: Iterable[CheckResult]) -> bool:
    ok = True
    for result in results:
        print(result.line())
        ok = ok and result.ok
    return ok
