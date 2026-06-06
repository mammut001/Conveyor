from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production installs python-dotenv
    def load_dotenv(env_file: str | Path = ".env") -> bool:
        path = Path(env_file)
        if not path.exists():
            return False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return True


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_allowed_user_id: int
    codex_workspace_root: Path
    codex_bin: str
    codex_task_root: Path
    codex_model: str | None
    codex_timeout_seconds: int
    telegram_progress_seconds: int
    codex_retry_429_delays_seconds: tuple[int, ...]
    codex_memory_root: Path
    user_timezone: str
    # Operator profile (onboarding-A/C). Defaults match the project's
    # single-operator / zh-CN / terse / personal-scale assumption;
    # load_settings overrides from .env (OPERATOR_NAME/LANGUAGE/
    # STYLE/STANDING) and then from operator.json if present
    # (onboarding-C wins for persistence). Fields live at the END
    # with defaults so existing Settings(...) positional callers in
    # the smokes keep working unchanged.
    operator_name: str | None = None
    operator_language: str = "zh-CN"
    operator_style: str = "terse"
    operator_standing: str = "personal-scale, single operator"


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int_env(name: str, default: int | None = None) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        if default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _int_list_env(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    delays: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            delay = int(part)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a comma-separated list of integers") from exc
        if delay < 0:
            raise RuntimeError(f"{name} values must be non-negative")
        delays.append(delay)
    return tuple(delays)


# Onboarding-C operator profile loader. Reads
# codex_memory_root/operator.json if it exists, and returns a dict
# of overrides for the 4 operator_* Settings fields. The JSON file
# is written by the /onboard Telegram conversation handler and
# is the source of truth for the operator's chosen identity once
# the user has completed first-run onboarding. .env values are the
# deployment-time defaults; operator.json wins when both are set.
# Stale or unknown fields in the JSON are silently dropped so a
# hand-edited or older profile file can't break load_settings.
OPERATOR_PROFILE_FIELDS = (
    "operator_name",
    "operator_language",
    "operator_style",
    "operator_standing",
)


def _load_operator_profile(memory_root: Path) -> dict[str, str | None]:
    path = memory_root / "operator.json"
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return {}
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: data[key]
        for key in OPERATOR_PROFILE_FIELDS
        if key in data and isinstance(data[key], (str, type(None)))
    }


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)

    workspace_root = Path(_required("CODEX_WORKSPACE_ROOT")).expanduser().resolve()
    task_root = Path(os.getenv("CODEX_TASK_ROOT", workspace_root.parent / "codex-telegram-runner")).expanduser().resolve()
    memory_root = Path(os.getenv("CODEX_MEMORY_ROOT", "~/.codex")).expanduser().resolve()
    (memory_root / "JOURNAL").mkdir(parents=True, exist_ok=True)
    (memory_root / "snapshots").mkdir(parents=True, exist_ok=True)
    (memory_root / "state").mkdir(parents=True, exist_ok=True)
    user_timezone = os.getenv("USER_TIMEZONE", "America/Toronto")

    # Operator profile (onboarding-C). Resolution order:
    # 1. operator.json (explicit operator choice from /onboard)
    # 2. .env OPERATOR_* (deployment-time override)
    # 3. dataclass default (project assumption)
    # operator.json wins because it represents the operator's
    # explicit choice and must survive across deploys/env changes.
    # operator_name is None-friendly: an empty string from
    # operator.json or env becomes None so the _operator_profile_text
    # renderer can fall back to the "(anonymous)" placeholder.
    profile = _load_operator_profile(memory_root)
    operator_name_env = os.getenv("OPERATOR_NAME") or None
    operator_language_env = os.getenv("OPERATOR_LANGUAGE", "zh-CN")
    operator_style_env = os.getenv("OPERATOR_STYLE", "terse")
    operator_standing_env = os.getenv("OPERATOR_STANDING", "personal-scale, single operator")
    operator_name = profile.get("operator_name", operator_name_env) or operator_name_env
    operator_language = profile.get("operator_language", operator_language_env) or operator_language_env
    operator_style = profile.get("operator_style", operator_style_env) or operator_style_env
    operator_standing = profile.get("operator_standing", operator_standing_env) or operator_standing_env

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_id=_int_env("TELEGRAM_ALLOWED_USER_ID"),
        codex_workspace_root=workspace_root,
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        codex_task_root=task_root,
        codex_model=os.getenv("CODEX_MODEL") or None,
        codex_timeout_seconds=_int_env("CODEX_TIMEOUT_SECONDS", 3600),
        telegram_progress_seconds=_int_env("TELEGRAM_PROGRESS_SECONDS", 3),  # chat feel: 3s instead of 20s
        codex_retry_429_delays_seconds=_int_list_env("CODEX_RETRY_429_DELAYS_SECONDS", (300, 900, 1800)),
        codex_memory_root=memory_root,
        user_timezone=user_timezone,
        operator_name=operator_name,
        operator_language=operator_language,
        operator_style=operator_style,
        operator_standing=operator_standing,
    )
