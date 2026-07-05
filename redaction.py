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

import logging
import traceback

class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_text(value) if isinstance(value, str) else value for key, value in record.args.items()}
        
        if record.exc_info:
            if not record.exc_text:
                try:
                    record.exc_text = redact_text("".join(traceback.format_exception(*record.exc_info)))
                except Exception:
                    pass
            record.exc_info = None
        
        if isinstance(record.exc_text, str):
            record.exc_text = redact_text(record.exc_text)
        
        if isinstance(record.stack_info, str):
            record.stack_info = redact_text(record.stack_info)
            
        return True

