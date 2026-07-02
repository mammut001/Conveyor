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


SENSITIVE_FIELDS = frozenset({
    "telegram_bot_token", "lark_app_secret", "gmail_app_password",
    "google_client_secret_path",  # path may hint at project layout
    "github_token",
    "web_search_api_key",
    # Future shared secret for a local desktop agent. Never echoed
    # in repr/chat/logs even though it is optional. The value is
    # only consulted when ``conveyor_desktop_node_enabled`` is true.
    "conveyor_desktop_agent_token",
})


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
    # Gmail App Password backend (P3.3). All optional; gmail.status reports
    # missing config gracefully. OAuth is a future phase.
    gmail_backend: str | None = None  # "imap_smtp" or None
    gmail_address: str | None = None
    gmail_app_password: str | None = None  # 16-char App Password, never exposed
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_smtp_host: str = "smtp.gmail.com"
    gmail_smtp_port: int = 587
    # Google OAuth (P3.4). Calendar + Contacts read tools.
    # Gmail remains App Password backend; OAuth only for Calendar/Contacts.
    google_client_secret_path: str | None = None  # path to client_secret_NNNN.json
    google_token_path: str | None = None  # default: codex_memory_root/secrets/google_token.json
    google_oauth_scopes: str | None = None  # comma-separated; default covers calendar+contacts
    google_oauth_redirect_port: int = 8765
    # GitHub (P3.6). Issues / PRs / CI read-first tools.
    github_token: str | None = None  # GitHub Personal Access Token, never exposed
    github_default_repo: str | None = None  # e.g. "mammut001/Conveyor"
    github_api_base: str = "https://api.github.com"
    # Web Fetch (P4.1). READ-only curl wrapper with strict URL validation.
    web_fetch_enabled: bool = True
    web_fetch_timeout_seconds: int = 10
    web_fetch_max_bytes: int = 2000000
    web_fetch_max_redirects: int = 3
    web_user_agent: str = "ConveyorBot/0.1"
    # Web Search (P4.1). Disabled by default; supports searxng/brave/tavily/serper.
    web_search_backend: str = "disabled"  # disabled|searxng|brave|tavily|serper
    web_search_api_key: str | None = None
    web_search_endpoint: str | None = None
    web_search_max_results: int = 8
    # Research (P4.1). Hybrid web.search + fetch + Codex synthesis.
    research_max_sources: int = 5
    research_fetch_top_n: int = 5
    research_max_chars_per_source: int = 6000
    # File Search / Knowledge Base (P4.2). Natural-language-first file search.
    file_search_enabled: bool = True
    file_search_allowed_roots: str | None = None  # comma-separated extra roots
    file_search_max_file_bytes: int = 1000000
    file_search_max_results: int = 10
    file_search_extensions: str = ".md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml"
    kb_root: str | None = None  # default: codex_memory_root/kb
    kb_index_path: str | None = None  # default: codex_memory_root/kb_index.sqlite
    # Execution nodes: phase-0 foundation. The VPS node is always
    # online (the control plane runs on it). The desktop node is
    # opt-in: setting ``conveyor_desktop_node_enabled=True`` makes
    # the registry list it, but it is still ``offline`` until a
    # future local desktop agent is wired up. Real screenshot /
    # mouse / keyboard / Computer Use is intentionally not
    # implemented in this task — see ``docs/desktop_security.md``.
    conveyor_desktop_node_enabled: bool = False
    conveyor_desktop_node_id: str | None = None
    conveyor_desktop_node_name: str | None = None
    conveyor_desktop_agent_token: str | None = None  # SENSITIVE
    conveyor_computer_use_default_mode: str = "observe_only"

    def __repr__(self) -> str:
        """Redact sensitive fields in repr."""
        fields = []
        for f in self.__dataclass_fields__.values():
            value = getattr(self, f.name)
            if f.name in SENSITIVE_FIELDS and value:
                fields.append(f"{f.name}='[REDACTED]'")
            else:
                fields.append(f"{f.name}={value!r}")
        return f"Settings({', '.join(fields)})"


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
        # Gmail App Password backend (P3.3)
        "gmail_backend": os.getenv("GMAIL_BACKEND") or None,
        "gmail_address": os.getenv("GMAIL_ADDRESS") or None,
        "gmail_app_password": os.getenv("GMAIL_APP_PASSWORD") or None,
        "gmail_imap_host": os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com"),
        "gmail_imap_port": _int_env("GMAIL_IMAP_PORT", 993),
        "gmail_smtp_host": os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com"),
        "gmail_smtp_port": _int_env("GMAIL_SMTP_PORT", 587),
        # Google OAuth (P3.4)
        "google_client_secret_path": os.getenv("GOOGLE_CLIENT_SECRET_PATH") or None,
        "google_token_path": os.getenv("GOOGLE_TOKEN_PATH") or None,
        "google_oauth_scopes": os.getenv("GOOGLE_OAUTH_SCOPES") or None,
        "google_oauth_redirect_port": _int_env("GOOGLE_OAUTH_REDIRECT_PORT", 8765),
        # GitHub (P3.6)
        "github_token": os.getenv("GITHUB_TOKEN") or None,
        "github_default_repo": os.getenv("GITHUB_DEFAULT_REPO") or None,
        "github_api_base": os.getenv("GITHUB_API_BASE", "https://api.github.com"),
        # Web Fetch (P4.1)
        "web_fetch_enabled": os.getenv("WEB_FETCH_ENABLED", "true").strip().lower() in ("true", "1", "yes"),
        "web_fetch_timeout_seconds": _int_env("WEB_FETCH_TIMEOUT_SECONDS", 10),
        "web_fetch_max_bytes": _int_env("WEB_FETCH_MAX_BYTES", 2000000),
        "web_fetch_max_redirects": _int_env("WEB_FETCH_MAX_REDIRECTS", 3),
        "web_user_agent": os.getenv("WEB_USER_AGENT", "ConveyorBot/0.1"),
        # Web Search (P4.1)
        "web_search_backend": os.getenv("WEB_SEARCH_BACKEND", "disabled").strip().lower(),
        "web_search_api_key": os.getenv("WEB_SEARCH_API_KEY") or None,
        "web_search_endpoint": os.getenv("WEB_SEARCH_ENDPOINT") or None,
        "web_search_max_results": _int_env("WEB_SEARCH_MAX_RESULTS", 8),
        # Research (P4.1)
        "research_max_sources": _int_env("RESEARCH_MAX_SOURCES", 5),
        "research_fetch_top_n": _int_env("RESEARCH_FETCH_TOP_N", 5),
        "research_max_chars_per_source": _int_env("RESEARCH_MAX_CHARS_PER_SOURCE", 6000),
        # File Search / Knowledge Base (P4.2)
        "file_search_enabled": os.getenv("FILE_SEARCH_ENABLED", "true").strip().lower() in ("true", "1", "yes"),
        "file_search_allowed_roots": os.getenv("FILE_SEARCH_ALLOWED_ROOTS") or None,
        "file_search_max_file_bytes": _int_env("FILE_SEARCH_MAX_FILE_BYTES", 1000000),
        "file_search_max_results": _int_env("FILE_SEARCH_MAX_RESULTS", 10),
        "file_search_extensions": os.getenv("FILE_SEARCH_EXTENSIONS", ".md,.txt,.py,.ts,.tsx,.js,.json,.yaml,.yml,.toml"),
        "kb_root": os.getenv("KB_ROOT") or None,
        "kb_index_path": os.getenv("KB_INDEX_PATH") or None,
        # Execution nodes (phase 0). All optional; default is
        # VPS-only with Computer Use stubbed. None of these affect
        # existing Telegram/Feishu/Codex behaviour.
        "conveyor_desktop_node_enabled": os.getenv("CONVEYOR_DESKTOP_NODE_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on"),
        "conveyor_desktop_node_id": os.getenv("CONVEYOR_DESKTOP_NODE_ID") or None,
        "conveyor_desktop_node_name": os.getenv("CONVEYOR_DESKTOP_NODE_NAME") or None,
        "conveyor_desktop_agent_token": os.getenv("CONVEYOR_DESKTOP_AGENT_TOKEN") or None,
        "conveyor_computer_use_default_mode": os.getenv(
            "CONVEYOR_COMPUTER_USE_DEFAULT_MODE", "observe_only",
        ).strip().lower() or "observe_only",
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
