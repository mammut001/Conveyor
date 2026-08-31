"""Safe, minimal editing of the active Codex provider configuration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_TOP_LEVEL_KEYS = ("model", "model_provider", "model_reasoning_effort")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_ASSIGNMENT_RE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"((?:\\.|[^"\\])*)"\s*(?:#.*)?$')
_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")


def _decode_toml_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (ValueError, json.JSONDecodeError):
        return value


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def config_path(settings: Any) -> Path:
    return Path(settings.codex_memory_root).expanduser().resolve() / "config.toml"


def env_path() -> Path:
    configured = os.getenv("CONVEYOR_ENV_FILE")
    return Path(configured).expanduser().resolve() if configured else (Path.cwd() / ".env").resolve()


def _parse_simple_config(text: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    top: dict[str, str] = {}
    providers: dict[str, dict[str, str]] = {}
    section = ""
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
            continue
        assignment = _ASSIGNMENT_RE.match(line)
        if not assignment:
            continue
        key, raw = assignment.groups()
        value = _decode_toml_string(raw)
        if not section:
            top[key] = value
        elif section.startswith("model_providers."):
            provider_id = section[len("model_providers."):]
            providers.setdefault(provider_id, {})[key] = value
    return top, providers


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    prefix = f"{key}="
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith(prefix):
            return raw[len(prefix):].strip().strip("'\"") or None
    return None


def _config_revision(values: dict[str, str]) -> str:
    """Stable non-secret fingerprint used to scope provider health state."""
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get_provider_config(settings: Any) -> dict[str, Any]:
    path = config_path(settings)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    top, providers = _parse_simple_config(text)
    provider_id = top.get("model_provider", "")
    provider = providers.get(provider_id, {})
    env_key = provider.get("env_key") or "OPENAI_API_KEY"
    secret = os.getenv(env_key) or _read_env_value(env_path(), env_key)
    effective = {
        "provider_id": provider_id,
        "provider_name": provider.get("name", provider_id),
        "model": top.get("model", ""),
        "reasoning_effort": top.get("model_reasoning_effort", "minimal"),
        "base_url": provider.get("base_url", ""),
        "wire_api": provider.get("wire_api", "responses"),
        "env_key": env_key,
    }
    return {
        **effective,
        "config_revision": _config_revision(effective),
        "api_key_configured": bool(secret),
        "api_key_hint": f"••••{secret[-4:]}" if secret and len(secret) >= 4 else ("••••" if secret else ""),
        "config_path": str(path),
    }


def _validate(payload: dict[str, Any]) -> dict[str, str]:
    provider_id = str(payload.get("provider_id") or "").strip().lower()
    provider_name = str(payload.get("provider_name") or provider_id).strip()
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    wire_api = str(payload.get("wire_api") or "responses").strip().lower()
    reasoning = str(payload.get("reasoning_effort") or "minimal").strip().lower()
    env_key = str(payload.get("env_key") or "OPENAI_API_KEY").strip().upper()
    api_key = str(payload.get("api_key") or "").strip()
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise ValueError("provider_id must start with a letter and contain only a-z, 0-9, _ or -")
    if not provider_name or len(provider_name) > 80:
        raise ValueError("provider_name must be 1-80 characters")
    if not model or len(model) > 120:
        raise ValueError("model must be 1-120 characters")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be an http(s) URL without embedded credentials")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("remote provider base_url must use https")
    if wire_api not in ("responses", "chat"):
        raise ValueError("wire_api must be responses or chat")
    if reasoning not in ("none", "minimal", "low", "medium", "high", "xhigh"):
        raise ValueError("unsupported reasoning effort")
    if not _ENV_KEY_RE.fullmatch(env_key):
        raise ValueError("invalid environment key name")
    if api_key and (len(api_key) < 8 or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key):
        raise ValueError("api_key must be 8-4096 characters on one line")
    return {
        "provider_id": provider_id, "provider_name": provider_name, "model": model,
        "base_url": base_url, "wire_api": wire_api, "reasoning_effort": reasoning,
        "env_key": env_key, "api_key": api_key,
    }


def _replace_provider_config(text: str, values: dict[str, str]) -> str:
    output: list[str] = []
    section = ""
    skip_section = False
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
            skip_section = section == f"model_providers.{values['provider_id']}"
            if skip_section:
                continue
        if skip_section:
            continue
        assignment = _ASSIGNMENT_RE.match(line)
        if not section and assignment and assignment.group(1) in _TOP_LEVEL_KEYS:
            continue
        output.append(line)
    remainder = "\n".join(output).strip()
    header = "\n".join([
        f"model = {_toml_string(values['model'])}",
        f"model_provider = {_toml_string(values['provider_id'])}",
        f"model_reasoning_effort = {_toml_string(values['reasoning_effort'])}",
        "",
        f"[model_providers.{values['provider_id']}]",
        f"name = {_toml_string(values['provider_name'])}",
        f"base_url = {_toml_string(values['base_url'])}",
        f"env_key = {_toml_string(values['env_key'])}",
        f"wire_api = {_toml_string(values['wire_api'])}",
    ])
    return header + ("\n\n" + remainder if remainder else "") + "\n"


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _update_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    prefix = f"{key}="
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                updated.append(prefix + value)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(prefix + value)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    _atomic_write(path, "\n".join(updated) + "\n", existing_mode)


def save_provider_config(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    values = _validate(payload)
    existing_key = os.getenv(values["env_key"]) or _read_env_value(env_path(), values["env_key"])
    if not values["api_key"] and not existing_key:
        raise ValueError("api_key is required because no key is currently configured")
    path = config_path(settings)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    _atomic_write(path, _replace_provider_config(existing, values))
    if values["api_key"]:
        _update_env(env_path(), values["env_key"], values["api_key"])
        os.environ[values["env_key"]] = values["api_key"]
    return get_provider_config(settings)


def refresh_provider_env() -> dict[str, str]:
    """Read provider credentials from .env for each new Codex child process."""
    result: dict[str, str] = {}
    path = env_path()
    if not path.exists():
        return result
    allowed = ("OPENAI_", "AZURE_OPENAI_", "MINIMAX_", "ANTHROPIC_", "DEEPSEEK_")
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key.startswith(allowed) and _ENV_KEY_RE.fullmatch(key):
            result[key] = value.strip().strip("'\"")
    return result
