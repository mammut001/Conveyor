"""handlers/onboarding.py — onboarding helper logic (no Telegram SDK).

Pure helpers for operator profile management.  The Telegram-specific
ConversationHandler and handler steps live in bot.py (P2.3: extracted
from the monolithic onboarding block but still in the entrypoint
because they need Telegram SDK types).

This module intentionally does NOT import telegram or telegram.ext
so it passes the import_boundary_smoke check.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from config import OPERATOR_PROFILE_FIELDS, Settings

logger = logging.getLogger("conveyor.onboarding")

OPERATOR_PROFILE_FILENAME = "operator.json"


def operator_profile_path(settings: Settings) -> Path:
    return settings.codex_memory_root / OPERATOR_PROFILE_FILENAME


def operator_profile_exists(settings: Settings) -> bool:
    return operator_profile_path(settings).exists()


def save_operator_profile(settings: Settings, data: dict) -> bool:
    """Write operator.json with the 4 known fields.  Returns True on
    success, False on OSError.  Stale/unknown fields are silently
    dropped."""
    path = operator_profile_path(settings)
    try:
        import os
        import tempfile
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

        fd, tmp_file_path = tempfile.mkstemp(dir=str(path.parent), prefix=".operator.json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {k: data[k] for k in OPERATOR_PROFILE_FIELDS if k in data},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_file_path, 0o600)
            os.replace(tmp_file_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_file_path)
            except OSError:
                pass
            raise
        return True
    except OSError:
        logger.exception("Failed to write operator.json")
        return False


def profile_text(settings: Settings) -> str | None:
    """Return formatted profile text for /profile, or None if no profile."""
    if not operator_profile_exists(settings):
        return None
    try:
        content = operator_profile_path(settings).read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        return f"读 operator.json 失败：{exc}"
    text = "当前 profile（`codex_memory_root/operator.json`）：\n"
    for key, label in (
        ("operator_name", "name"),
        ("operator_language", "language"),
        ("operator_style", "style"),
        ("operator_standing", "standing"),
    ):
        val = data.get(key)
        text += f"  {label}: {val if val is not None else '(unset)'}\n"
    text += "\n重做问卷 `/onboard`。"
    return text
