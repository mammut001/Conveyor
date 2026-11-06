from __future__ import annotations

import json
import os
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


def _pi_backend_identity(settings: Settings) -> tuple[str, str, str]:
    """Resolve (pi_bin, provider, model) exactly like ``PiBackend.__init__``.

    Pure helper (no heavy ``PiBackend`` construction) so doctor can report the
    same provider/model/binary the production Pi run would use.
    """
    from runner.pi_agent import normalize_pi_model

    pi_bin = str(
        os.getenv("CONVEYOR_PI_BIN")
        or getattr(settings, "pi_bin", "")
        or "pi"
    ).strip() or "pi"
    provider = str(
        os.getenv("CONVEYOR_PI_PROVIDER")
        or getattr(settings, "pi_provider", "")
        or "deepseek"
    ).strip().lower() or "deepseek"
    raw_model = str(
        os.getenv("CONVEYOR_PI_MODEL")
        or getattr(settings, "pi_model", "")
        or getattr(settings, "chat_model", "")
        or "deepseek-v4-flash"
    ).strip()
    model = normalize_pi_model(provider, raw_model)
    return pi_bin, provider, model


def check_pi_backend(settings: Settings) -> list[CheckResult]:
    """Probe the Pi agent backend when it is the active agent backend.

    Only runs when ``CONVEYOR_AGENT_BACKEND=pi``; otherwise returns ``[]``.
    Checks the pi binary, its ``models.json`` provider/model declaration, and
    the selected provider's API key. Never prints a key value.
    """
    if os.getenv("CONVEYOR_AGENT_BACKEND", "codex").strip().lower() != "pi":
        return []

    from runner.pi_agent import pi_api_key_env

    pi_bin, provider, model = _pi_backend_identity(settings)
    results: list[CheckResult] = []

    # pi binary
    resolved = shutil.which(pi_bin)
    if resolved is None:
        candidate = Path(pi_bin).expanduser()
        if candidate.exists():
            resolved = str(candidate)
    if resolved:
        results.append(CheckResult("pi binary", True, resolved))
    else:
        results.append(CheckResult("pi binary", False, f"{pi_bin} not found on PATH"))

    # pi models.json
    models_path = Path(settings.codex_task_root) / "pi-agent" / "models.json"
    if not models_path.exists():
        results.append(CheckResult("pi models.json", True, "absent (pi built-in providers)"))
    else:
        try:
            raw = models_path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            results.append(CheckResult("pi models.json", False, f"invalid JSON: {exc}"))
            data = None
        if isinstance(data, dict):
            providers = data.get("providers")
            if not isinstance(providers, dict) or provider not in providers:
                results.append(
                    CheckResult("pi models.json", False, f"provider {provider} not declared")
                )
            else:
                entry = providers.get(provider) or {}
                models = entry.get("models") if isinstance(entry, dict) else None
                ids = [
                    str(m.get("id"))
                    for m in (models or [])
                    if isinstance(m, dict) and m.get("id")
                ]
                if model and ids and model not in ids:
                    results.append(
                        CheckResult(
                            "pi models.json",
                            False,
                            f"model {model} not declared for {provider}",
                        )
                    )
                else:
                    detail_ids = ",".join(ids) if ids else "(none)"
                    results.append(
                        CheckResult(
                            "pi models.json",
                            True,
                            f"provider={provider} models={detail_ids}",
                        )
                    )
        elif data is not None:
            results.append(CheckResult("pi models.json", False, "not a JSON object"))

    # pi api key
    key_env = pi_api_key_env(provider)
    if str(os.environ.get(key_env) or "").strip():
        results.append(CheckResult("pi api key", True, f"{key_env} set"))
    else:
        stored = ""
        try:
            from console_secrets import path_for, read_secret

            secrets_path = getattr(settings, "secrets_file", None)
            if secrets_path is None and getattr(settings, "codex_memory_root", None) is not None:
                secrets_path = path_for(settings)
            if secrets_path is not None:
                stored = str(read_secret(secrets_path, key_env) or "").strip()
        except Exception:
            stored = ""
        if stored:
            results.append(CheckResult("pi api key", True, f"{key_env} set (console secret)"))
        else:
            results.append(CheckResult("pi api key", False, f"{key_env} missing"))

    return results


def check_chat_provider(settings: Settings) -> CheckResult | None:
    """Probe the chat-tier provider when ``chat_mode=auto``; else ``None``.

    Uses the same chat config resolution as ``runner.chat_client`` and probes
    ``GET {base_url}/models``. Never includes the API key in any detail.
    """
    if str(getattr(settings, "chat_mode", "off") or "off").strip().lower() != "auto":
        return None

    from runner.chat_client import config_from_settings

    config = config_from_settings(settings)
    if config is None:
        return CheckResult("chat provider", False, "chat_mode=auto but chat key/model missing")

    base_url = str(config.base_url).rstrip("/")
    model = config.model
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {config.api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = getattr(response, "status", None) or response.getcode()
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        return CheckResult("chat provider", False, f"{base_url}/models HTTP {exc.code}")
    except Exception as exc:
        return CheckResult("chat provider", False, f"{base_url}/models failed: {exc.__class__.__name__}")

    if status is not None and int(status) != 200:
        return CheckResult("chat provider", False, f"{base_url}/models HTTP {status}")

    entries = data.get("data") if isinstance(data, dict) else None
    if isinstance(entries, list) and entries:
        model_ids = {item.get("id") for item in entries if isinstance(item, dict)}
        if model not in model_ids:
            preview = ", ".join(sorted(x for x in model_ids if x)[:12])
            return CheckResult(
                "chat provider", False, f"model {model} not listed by provider; saw: {preview}"
            )
    return CheckResult("chat provider", True, f"model={model} listed")


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
        if not isinstance(event, dict):
            # Skip non-object lines (e.g. JSON lists or strings) instead of crashing.
            continue
        if event.get("type") == "turn.completed":
            return True
        if event.get("kind") == "assistant.completed":
            return True
    return False


def print_results(results: Iterable[CheckResult]) -> bool:
    ok = True
    for result in results:
        print(result.line())
        ok = ok and result.ok
    return ok
