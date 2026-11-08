"""Safe, minimal editing of the active Codex provider configuration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
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
    revision = _config_revision(effective)
    result: dict[str, Any] = {
        **effective,
        "config_revision": revision,
        "api_key_configured": bool(secret),
        "api_key_hint": f"••••{secret[-4:]}" if secret and len(secret) >= 4 else ("••••" if secret else ""),
        "config_path": str(path),
    }
    try:
        from provider_health import get_provider_health
        result["health"] = get_provider_health(settings).snapshot(provider_id, revision)
    except Exception:
        result["health"] = {
            "provider_id": provider_id or "unknown", "status": "unknown",
            "circuit_open": False, "retry_after_seconds": 0,
            "consecutive_failures": 0, "last_error_kind": None,
            "last_error_at": None, "last_success_at": None,
        }
    return result


_CREDENTIAL_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN")
_RUNTIME_PREFIXES = ("LD_", "PYTHON", "CONVEYOR_", "CODEX_", "GIT_", "NODE_", "PI_", "SSL_",
                     "REQUESTS_", "CURL_", "HTTP", "NO_PROXY", "PATH", "HOME", "SHELL", "TELEGRAM_", "LARK_")


def _is_credential_name(name: str) -> bool:
    """Only provider credential variables may be written from the console;
    LD_PRELOAD, PATH, CONVEYOR_* and the like change how the host runs."""
    return name.endswith(_CREDENTIAL_SUFFIXES) and not name.startswith(_RUNTIME_PREFIXES)


def _validate(payload: dict[str, Any]) -> dict[str, str]:
    provider_id = str(payload.get("provider_id") or "").strip().lower()
    provider_name = str(payload.get("provider_name") or provider_id).strip()
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    # A save that omits wire_api gets Chat Completions; saved values are never rewritten here.
    wire_api = str(payload.get("wire_api") or "chat").strip().lower()
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
    if not _ENV_KEY_RE.fullmatch(env_key) or not _is_credential_name(env_key):
        raise ValueError("env_key must be a provider credential name such as OPENAI_API_KEY")
    # splitlines() (used by every .env reader) also breaks on \u2028, \x85,
    # \x0b ... so any control or line-separator character could smuggle a
    # second VAR=value line into .env.
    if api_key and (len(api_key) < 8 or len(api_key) > 4096
                    or any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "\x85\u2028\u2029" for ch in api_key)):
        raise ValueError("api_key must be 8-4096 printable characters on one line")
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
    # The file now holds a secret: never keep group/world access it may have had.
    existing_mode = stat.S_IMODE(path.stat().st_mode) & 0o600 if path.exists() else 0o600
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
    try:
        from provider_health import get_provider_health
        get_provider_health(settings).reset(values["provider_id"])
    except Exception:
        pass
    return get_provider_config(settings)


def get_saved_provider_credentials(settings: Any) -> dict[str, str] | None:
    """Return active provider {base_url, model, api_key} from config.toml and .env/os.environ."""
    try:
        path = config_path(settings)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        top, providers = _parse_simple_config(text)
        provider_id = top.get("model_provider", "").strip()
        model = top.get("model", "").strip()
        if not provider_id or not model:
            return None
        provider = providers.get(provider_id, {})
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            return None
        env_key = provider.get("env_key") or "OPENAI_API_KEY"
        secret = os.getenv(env_key) or _read_env_value(env_path(), env_key)
        if not secret:
            return None
        return {
            "base_url": base_url,
            "model": model,
            "api_key": secret,
        }
    except Exception:
        return None


def get_saved_provider(settings: Any) -> dict[str, str] | None:
    """Return the saved 模型设置 provider incl. wire_api/env_key and its key.

    {provider_id, provider_name, base_url, model, wire_api, env_key, api_key}
    or None when no usable provider (url, model and key) is saved. The key is
    returned for handing to a child process; callers must never log it.
    """
    try:
        path = config_path(settings)
        if not path.exists():
            return None
        top, providers = _parse_simple_config(path.read_text(encoding="utf-8", errors="replace"))
        provider_id = top.get("model_provider", "").strip()
        model = top.get("model", "").strip()
        provider = providers.get(provider_id, {})
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        if not provider_id or not model or not base_url:
            return None
        env_key = str(provider.get("env_key") or "OPENAI_API_KEY").strip()
        if not _ENV_KEY_RE.fullmatch(env_key):
            return None
        secret = os.getenv(env_key) or _read_env_value(env_path(), env_key)
        if not secret:
            return None
        wire_api = str(provider.get("wire_api") or "responses").strip().lower()
        return {
            "provider_id": provider_id,
            "provider_name": provider.get("name", provider_id),
            "base_url": base_url,
            "model": model,
            "wire_api": wire_api,
            "env_key": env_key,
            "api_key": secret,
        }
    except Exception:
        return None


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


class ProviderModelsError(Exception):
    pass


class ApiKeyRequiredError(ProviderModelsError):
    pass


def _normalize_base_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    default = {"https": 443, "http": 80}.get(parsed.scheme.lower())
    port = parsed.port if parsed.port is not None else default
    return f"{parsed.scheme.lower()}://{host}:{port}{parsed.path.rstrip('/')}"


def fetch_provider_models(settings: Any, draft: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """List models of a provider.

    Without `draft` the saved provider is used (GET). With `draft` the form
    values are used: base_url and env_key from the draft, api_key from the
    draft or, when blank, the key already stored for that env_key.
    """
    if draft is not None:
        base_url = str(draft.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ProviderModelsError("请先填写服务地址")
        env_key = str(draft.get("env_key") or "OPENAI_API_KEY").strip().upper()
        if not _ENV_KEY_RE.fullmatch(env_key):
            raise ProviderModelsError("密钥名称格式不正确")
        typed_key = str(draft.get("api_key") or "").strip()
        if typed_key and (len(typed_key) > 4096 or "\n" in typed_key or "\r" in typed_key):
            raise ProviderModelsError("API 密钥格式不正确")
        if typed_key:
            secret = typed_key
        else:
            # The stored key may only go to the saved provider itself.
            path = config_path(settings)
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            top, providers = _parse_simple_config(text)
            saved = providers.get(top.get("model_provider", ""), {})
            saved_url = str(saved.get("base_url") or "").strip()
            saved_env = str(saved.get("env_key") or "OPENAI_API_KEY").strip().upper()
            try:
                same_url = bool(saved_url) and _normalize_base_url(saved_url) == _normalize_base_url(base_url)
            except ValueError:
                same_url = False
            if not (same_url and saved_env == env_key):
                raise ApiKeyRequiredError("换了接口地址后，需要先填写 API 密钥才能读取模型列表")
            secret = os.getenv(env_key) or _read_env_value(env_path(), env_key)
    else:
        path = config_path(settings)
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        top, providers = _parse_simple_config(text)
        provider_id = top.get("model_provider", "")
        if not provider_id:
            raise ProviderModelsError("未配置模型供应商")
        provider = providers.get(provider_id, {})
        base_url = str(provider.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ProviderModelsError("未配置 Provider Base URL")
        env_key = provider.get("env_key") or "OPENAI_API_KEY"
        secret = os.getenv(env_key) or _read_env_value(env_path(), env_key)

    parsed = urlparse(base_url)
    if parsed.username or parsed.password or not parsed.netloc:
        raise ProviderModelsError("服务地址格式不正确")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        pass
    else:
        raise ProviderModelsError("远程供应商 Base URL 必须使用 HTTPS")

    models_url = f"{base_url}/models"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Conveyor/1.0",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    req = urllib.request.Request(models_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(2 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderModelsError(f"服务商拒绝了这个密钥（HTTP {exc.code}），请检查 API 密钥") from exc
        raise ProviderModelsError(f"供应商服务返回 HTTP {exc.code}，请检查服务地址") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderModelsError("请求超时或网络连接失败") from exc
    except Exception as exc:
        raise ProviderModelsError("解析模型列表失败") from exc

    models_list: list[dict[str, str]] = []
    seen = set()
    raw_list = []
    if isinstance(data, dict):
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            raw_list = data.get("models") or []
    elif isinstance(data, list):
        raw_list = data

    if isinstance(raw_list, list):
        for item in raw_list:
            mid = None
            if isinstance(item, dict) and "id" in item:
                mid = str(item["id"]).strip()
            elif isinstance(item, str):
                mid = item.strip()
            if mid and mid not in seen:
                seen.add(mid)
                models_list.append({"id": mid})

    return models_list


