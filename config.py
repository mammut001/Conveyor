from __future__ import annotations

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


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file)

    workspace_root = Path(_required("CODEX_WORKSPACE_ROOT")).expanduser().resolve()
    task_root = Path(os.getenv("CODEX_TASK_ROOT", workspace_root.parent / "codex-telegram-runner")).expanduser().resolve()
    memory_root = Path(os.getenv("CODEX_MEMORY_ROOT", "~/.codex")).expanduser().resolve()
    (memory_root / "JOURNAL").mkdir(parents=True, exist_ok=True)
    (memory_root / "snapshots").mkdir(parents=True, exist_ok=True)
    (memory_root / "state").mkdir(parents=True, exist_ok=True)
    user_timezone = os.getenv("USER_TIMEZONE", "America/Toronto")

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_id=_int_env("TELEGRAM_ALLOWED_USER_ID"),
        codex_workspace_root=workspace_root,
        codex_bin=os.getenv("CODEX_BIN", "codex"),
        codex_task_root=task_root,
        codex_model=os.getenv("CODEX_MODEL") or None,
        codex_timeout_seconds=_int_env("CODEX_TIMEOUT_SECONDS", 3600),
        telegram_progress_seconds=_int_env("TELEGRAM_PROGRESS_SECONDS", 20),
        codex_retry_429_delays_seconds=_int_list_env("CODEX_RETRY_429_DELAYS_SECONDS", (300, 900, 1800)),
        codex_memory_root=memory_root,
        user_timezone=user_timezone,
    )
