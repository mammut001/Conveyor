from __future__ import annotations

import re
import os
from collections.abc import Mapping, Sequence
from typing import Any

# Single source of truth for sensitive fields in Settings
SENSITIVE_SETTING_FIELDS = frozenset({
    "telegram_bot_token", 
    "lark_app_secret", 
    "gmail_app_password",
    "google_client_secret_path",  
    "github_token",
    "web_search_api_key",
    "conveyor_desktop_agent_token",
})

# Substrings to search for in dictionary keys to mark them sensitive
SECRET_KEY_SUBSTRINGS = (
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "authorization", "bearer", "session", "cookie", "private_key", "privatekey"
)

# Regex to match sensitive key names
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|bearer|session|cookie|private[_-]?key)",
    re.IGNORECASE,
)

# Regex patterns to detect secret values in arbitrary text blocks
SECRET_VALUE_PATTERNS = [
    re.compile(r"(api\.telegram\.org/bot)[A-Za-z0-9:_-]+"),
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(ya29\.)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(AIza)[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\b(cli_)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(t-)[a-zA-Z0-9_-]{20,}\b"),
    re.compile(r"\b(sk-ant-api)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(authorization:\s*)[^\s]+"),
    re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\b\s*[:=]\s*['\"]?[^'\"\s]+"),
]

# Env variables allowed to pass through to child processes exactly
ALLOWED_CHILD_ENV_EXACT = {
    "HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"
}

# Env prefix variables allowed to pass through
ALLOWED_CHILD_ENV_PREFIXES = {
    "CODEX_", "OPENAI_", "AZURE_OPENAI_", "MINIMAX_", "ANTHROPIC_"
}

def is_sensitive_key(key: str) -> bool:
    """Check if key is sensitive based on key name regex or substrings."""
    k = key.lower()
    if SECRET_KEY_RE.search(k):
        return True
    for sub in SECRET_KEY_SUBSTRINGS:
        if sub in k:
            return True
    return False

def redact_text(text: str) -> str:
    """Redact secret pattern values within raw text string."""
    if not isinstance(text, str):
        return text
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted

def redact_obj(value: Any) -> Any:
    """Recursively redact sensitive keys and values in a dictionary or sequence."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str in {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"}:
                result[key_str] = redact_obj(item)
            elif is_sensitive_key(key_str):
                result[key_str] = "[REDACTED]"
            else:
                result[key_str] = redact_obj(item)
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_obj(item) for item in value]
    return value

DENYLIST_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "LARK_APP_SECRET",
    "GMAIL_APP_PASSWORD",
    "GITHUB_TOKEN",
    "CONVEYOR_DESKTOP_AGENT_TOKEN",
    "WEB_SEARCH_API_KEY",
})


def child_env_from(os_environ: Mapping[str, str]) -> dict[str, str]:
    """Filter environment for child executions, adding support for CONVEYOR_CHILD_ENV_PREFIXES."""
    explicit_prefixes = set()
    for env_var in ("CONVEYOR_CHILD_ENV_PREFIXES", "CONVEYOR_CHILD_ENV_EXTRA_PREFIXES"):
        val = os_environ.get(env_var)
        if val:
            for part in val.split(","):
                part = part.strip()
                if part:
                    explicit_prefixes.add(part)

    allowed_prefixes = set(ALLOWED_CHILD_ENV_PREFIXES) | explicit_prefixes
    allowed_prefixes_tuple = tuple(allowed_prefixes)
    
    env: dict[str, str] = {}
    stripped: list[str] = []
    
    for key, value in os_environ.items():
        # Check if it matches allowed exact/prefix list
        is_allowed = (key in ALLOWED_CHILD_ENV_EXACT or key.startswith(allowed_prefixes_tuple))
        if is_allowed:
            # Denylist check: block if key in DENYLIST_KEYS or starts with GOOGLE_,
            # unless the prefix was explicitly configured by the user.
            is_explicit = any(key.startswith(pref) for pref in explicit_prefixes)
            is_blocked = (key in DENYLIST_KEYS or key.startswith("GOOGLE_")) and not is_explicit
            if is_blocked:
                stripped.append(key)
            else:
                env[key] = value
                
    audit_enabled = os_environ.get("CONVEYOR_CHILD_ENV_AUDIT", "true").lower() in ("true", "1", "yes")
    if audit_enabled and stripped:
        import logging
        logger = logging.getLogger("conveyor.security")
        logger.info("Stripped %d sensitive keys from child environment: %s", len(stripped), ", ".join(sorted(stripped)))
        
    return env
