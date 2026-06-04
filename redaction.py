from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


MAX_TELEGRAM_MESSAGE = 3900

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|bearer|session|cookie|private[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = [
    re.compile(r"(api\.telegram\.org/bot)[A-Za-z0-9:_-]+"),
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(authorization:\s*)[^\s]+"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]+"),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_obj(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"}:
                result[key_str] = redact_obj(item)
            elif SECRET_KEY_RE.search(key_str):
                result[key_str] = "[REDACTED]"
            else:
                result[key_str] = redact_obj(item)
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_obj(item) for item in value]
    return value


def safe_json(value: Any, limit: int = MAX_TELEGRAM_MESSAGE) -> str:
    text = json.dumps(redact_obj(value), ensure_ascii=False, indent=2)
    return truncate(text, limit)


def truncate(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> str:
    text = redact_text(text.strip())
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n...[truncated]"
