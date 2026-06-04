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


def check_minimax_models(settings: Settings) -> CheckResult:
    import os

    try:
        import tomllib
    except ModuleNotFoundError:
        tomllib = None

    key = os.getenv("MINIMAX_API_KEY")
    if not key:
        return CheckResult("minimax", False, "MINIMAX_API_KEY is not set")

    base_url = os.getenv("MINIMAX_BASE_URL")
    model = settings.codex_model
    config_path = Path.home() / ".codex" / "config.toml"
    if tomllib and config_path.exists():
        config = tomllib.loads(config_path.read_text())
        provider_id = config.get("model_provider")
        provider = config.get("model_providers", {}).get(provider_id, {})
        if provider_id == "minimax":
            base_url = base_url or provider.get("base_url")
            model = model or config.get("model")

    base_url = (base_url or "https://api.minimaxi.com/v1").rstrip("/")
    model = model or "MiniMax-M3"
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        return CheckResult("minimax", False, f"{base_url}/models HTTP {exc.code}: {body}")
    except Exception as exc:
        return CheckResult("minimax", False, f"{base_url}/models failed: {exc}")

    model_ids = {item.get("id") for item in data.get("data", []) if isinstance(item, dict)}
    if model not in model_ids:
        preview = ", ".join(sorted(x for x in model_ids if x)[:12])
        return CheckResult("minimax", False, f"{model} not listed at {base_url}; saw: {preview}")
    return CheckResult("minimax", True, f"{base_url} lists {model}")


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
