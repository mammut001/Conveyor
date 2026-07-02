from __future__ import annotations

from typing import Any
import security.secrets

MAX_TELEGRAM_MESSAGE = 3900

SECRET_KEY_RE = security.secrets.SECRET_KEY_RE
SECRET_VALUE_PATTERNS = security.secrets.SECRET_VALUE_PATTERNS

def redact_text(text: str) -> str:
    return security.secrets.redact_text(text)

def redact_obj(value: Any) -> Any:
    return security.secrets.redact_obj(value)

def safe_json(value: Any, limit: int = MAX_TELEGRAM_MESSAGE) -> str:
    import json
    text = json.dumps(redact_obj(value), ensure_ascii=False, indent=2)
    return truncate(text, limit)

def truncate(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> str:
    text = redact_text(text.strip())
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n...[truncated]"
