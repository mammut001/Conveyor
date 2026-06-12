from __future__ import annotations

import json
import logging
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


logger = logging.getLogger("conveyor.config")


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
    # Feishu/Lark bot (feishu_bot.py). Optional for Telegram-only deploys.
    lark_app_id: str | None = None
    lark_app_secret: str | None = None
    lark_allowed_open_id: str | None = None
    # Progress verbosity for the Codex streaming UX.
    # verbose → every Codex event (prose, tool indicator, thinking,
    #          tool-pulse) is forwarded to the chat (debug-friendly).
    # compact (default) → suppress intermediate agent prose; only
    #          tool indicators, thinking indicator, and tool-pulse
    #          reach the chat. The final summary is still sent.
    # quiet   → no intermediate progress at all; only the initial
    #          placeholder and the final summary reach the chat.
    # Invalid env values fall back to "compact" with a warning.
    conveyor_progress_mode: str = "compact"
    # Session summary: lightweight per-chat context for "继续" / "continue".
    # Stores recent turns in codex_memory_root/session/ as JSONL.
    # Not long-term memory — can be cleared with /forget.
    conveyor_session_enabled: bool = True
    conveyor_session_max_turns: int = 20
    conveyor_session_inject_turns: int = 5


VALID_PROGRESS_MODES = ("verbose", "compact", "quiet")
DEFAULT_PROGRESS_MODE = "compact"


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


def _progress_mode_env(name: str, default: str) -> str:
    """Read CONVEYOR_PROGRESS_MODE. Unknown values fall back to
    ``default`` with a logged warning; this is intentionally lenient
    so a typo in .env does not brick a deploy."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    if value not in VALID_PROGRESS_MODES:
        logger.warning(
            "%s=%r is not one of %s; falling back to %r",
            name, value, list(VALID_PROGRESS_MODES), default,
        )
        return default
    return value


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


def load_operator_profile(memory_root: Path) -> dict[str, str | None]:
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
    codex = _load_codex_fields(env_file)
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_id=_int_env("TELEGRAM_ALLOWED_USER_ID"),
        **codex,
    )


def _load_codex_fields(env_file: str | Path = ".env") -> dict:
    """Shared Codex/operator fields for Telegram and Feishu bots."""
    load_dotenv(env_file)

    workspace_root = Path(_required("CODEX_WORKSPACE_ROOT")).expanduser().resolve()
    task_root = Path(os.getenv("CODEX_TASK_ROOT", workspace_root.parent / "conveyor")).expanduser().resolve()
    memory_root = Path(os.getenv("CODEX_MEMORY_ROOT", "~/.codex")).expanduser().resolve()
    (memory_root / "JOURNAL").mkdir(parents=True, exist_ok=True)
    (memory_root / "snapshots").mkdir(parents=True, exist_ok=True)
    (memory_root / "state").mkdir(parents=True, exist_ok=True)
    user_timezone = os.getenv("USER_TIMEZONE", "America/Toronto")

    # Operator profile (onboarding-C). Resolution order:
    # 1. operator.json (explicit operator choice from /onboard)
    # 2. .env OPERATOR_* (deployment-time override)
    # 3. dataclass default (project assumption)
    profile = load_operator_profile(memory_root)
    operator_name_env = os.getenv("OPERATOR_NAME") or None
    operator_language_env = os.getenv("OPERATOR_LANGUAGE", "zh-CN")
    operator_style_env = os.getenv("OPERATOR_STYLE", "terse")
    operator_standing_env = os.getenv("OPERATOR_STANDING", "personal-scale, single operator")
    operator_name = profile.get("operator_name", operator_name_env) or operator_name_env
    operator_language = profile.get("operator_language", operator_language_env) or operator_language_env
    operator_style = profile.get("operator_style", operator_style_env) or operator_style_env
    operator_standing = profile.get("operator_standing", operator_standing_env) or operator_standing_env

    return {
        "codex_workspace_root": workspace_root,
        "codex_bin": os.getenv("CODEX_BIN", "codex"),
        "codex_task_root": task_root,
        "codex_model": os.getenv("CODEX_MODEL") or None,
        "codex_timeout_seconds": _int_env("CODEX_TIMEOUT_SECONDS", 3600),
        "telegram_progress_seconds": _int_env("TELEGRAM_PROGRESS_SECONDS", 3),
        "codex_retry_429_delays_seconds": _int_list_env("CODEX_RETRY_429_DELAYS_SECONDS", (300, 900, 1800)),
        "codex_memory_root": memory_root,
        "user_timezone": user_timezone,
        "operator_name": operator_name,
        "operator_language": operator_language,
        "operator_style": operator_style,
        "operator_standing": operator_standing,
        "conveyor_progress_mode": _progress_mode_env(
            "CONVEYOR_PROGRESS_MODE", DEFAULT_PROGRESS_MODE,
        ),
        "conveyor_session_enabled": os.getenv("CONVEYOR_SESSION_ENABLED", "true").strip().lower() in ("true", "1", "yes"),
        "conveyor_session_max_turns": _int_env("CONVEYOR_SESSION_MAX_TURNS", 20),
        "conveyor_session_inject_turns": _int_env("CONVEYOR_SESSION_INJECT_TURNS", 5),
    }


def load_feishu_settings(env_file: str | Path = ".env") -> Settings:
    """Load settings for feishu_bot.py (Codex + Lark credentials)."""
    codex = _load_codex_fields(env_file)
    allowed = os.getenv("LARK_ALLOWED_OPEN_ID", "").strip() or None
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "feishu-only-unused"),
        telegram_allowed_user_id=_int_env("TELEGRAM_ALLOWED_USER_ID", 0),
        lark_app_id=_required("LARK_APP_ID"),
        lark_app_secret=_required("LARK_APP_SECRET"),
        lark_allowed_open_id=allowed,
        **codex,
    )
