"""Explicit, opt-in allowlist for agent and job child process environments.

Never expand a prefix from the parent environment. Grant exactly the credential
needed by the selected backend; a wildcard cannot grant any credential.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Iterable
from typing import Any

logger = logging.getLogger("conveyor.security")

BASE = frozenset({
    "PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
})
CODEX = frozenset({
    "CODEX_HOME", "CODEX_RUNNER_HOME", "CODEX_WORKSPACE_ROOT",
    "CODEX_TELEGRAM_JOB", "CODEX_TASK_ROOT",
})
MODEL_CONFIG = frozenset({
    "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
    "OPENAI_MODEL", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL", "MINIMAX_BASE_URL", "MINIMAX_MODEL",
})
DESKTOP = frozenset({
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE",
})
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SENSITIVE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|CREDENTIAL|AUTHORIZATION|COOKIE|SESSION",
    re.IGNORECASE,
)
_BLOCKED = re.compile(
    r"^(TELEGRAM_|FEISHU_|LARK_|GITHUB_|GH_TOKEN$|AWS_|GOOGLE_|GCP_|"
    r"CONVEYOR_.*(?:SECRET|TOKEN|PASSWORD|KEY)|GMAIL_)",
    re.IGNORECASE,
)
# Only provider credential names can be granted. No GitHub, bot, web or cloud
# control-plane credential is eligible, even when requested by a caller.
_PROVIDER = re.compile(
    r"^(?:OPENAI_|ANTHROPIC_|DEEPSEEK_|MINIMAX_|AZURE_OPENAI_)[A-Z0-9_]*(?:API_KEY|AUTH_TOKEN)$"
)

def enabled(settings: Any = None) -> bool:
    if settings is not None and hasattr(settings, "env_isolation"):
        return bool(settings.env_isolation)
    return os.environ.get("CONVEYOR_ENV_ISOLATION", "").strip().lower() in ("1", "true", "yes", "on")


def allowed_grant(name: str) -> bool:
    return bool(_NAME.fullmatch(name) and _PROVIDER.fullmatch(name) and not _BLOCKED.search(name))


def sensitive(name: str) -> bool:
    return bool(_SENSITIVE.search(name) or _BLOCKED.search(name))


def build_child_env(
    source: Mapping[str, str] | None = None,
    *,
    grants: Iterable[str] = (),
    extra: Iterable[str] = (),
    overrides: Mapping[str, str] | None = None,
    secrets_file: Any = None,
) -> dict[str, str]:
    """Construct environment using named allowlists, never arbitrary prefixes.

    extras are only non-secret names; overrides are also checked. No source
    credential is inherited unless it is in the explicit provider grant set.
    """
    src = os.environ if source is None else source
    granted = {key for key in grants if isinstance(key, str) and allowed_grant(key)}
    extra_safe = {key for key in extra if isinstance(key, str) and _NAME.fullmatch(key) and not sensitive(key)}
    allowed = BASE | CODEX | MODEL_CONFIG | extra_safe
    out = {
        key: str(value) for key, value in src.items()
        if key in allowed or (key.startswith("LC_") and not sensitive(key))
    }
    for key in granted:
        if key in src:
            out[key] = str(src[key])
        elif secrets_file is not None:
            from console_secrets import read_secret
            stored = read_secret(secrets_file, key)
            if stored is not None:
                out[key] = stored
    for key, value in (overrides or {}).items():
        if key in allowed or (key.startswith("LC_") and not sensitive(key)):
            out[key] = str(value)
        elif key in granted:
            out[key] = str(value)
    # Safety net independent of the allowlist: future additions cannot leak.
    return {
        key: value for key, value in out.items()
        if (not sensitive(key) or key in granted)
        and not (_BLOCKED.search(key) and key not in granted)
    }


def model_grants(settings: Any, *, claude: bool = False) -> set[str]:
    if claude:
        return {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    try:
        from provider_config import get_provider_config
        config = get_provider_config(settings)
        name = str(config.get("env_key") or "OPENAI_API_KEY")
        return {name} if allowed_grant(name) else set()
    except Exception:
        return {"OPENAI_API_KEY"}


def agent_env(settings: Any, source: Mapping[str, str] | None = None,
              *, claude: bool = False, overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Codex/Claude child env (call only while opt-in isolation is enabled)."""
    from console_secrets import path_for
    secrets_path = getattr(settings, "secrets_file", None)
    if secrets_path is None and getattr(settings, "codex_memory_root", None) is not None:
        secrets_path = path_for(settings)
    return build_child_env(
        source, grants=model_grants(settings, claude=claude), overrides=overrides,
        secrets_file=secrets_path,
    )


def utility_env(settings: Any, source: Mapping[str, str] | None = None,
                *, desktop: bool = False) -> dict[str, str] | None:
    """None keeps subprocess's existing inherited environment when flag is off."""
    if not enabled(settings):
        return None
    return build_child_env(source, extra=DESKTOP if desktop else ())


def desktop_env(source: Mapping[str, str]) -> dict[str, str]:
    """For desktop adapters without Settings, opt in via the same process flag."""
    return build_child_env(source, extra=DESKTOP) if enabled() else dict(source)


def log_startup_check(settings: Any, source: Mapping[str, str] | None = None) -> None:
    """Names only; never log values or the contents of the secrets file."""
    if not enabled(settings):
        return
    child = agent_env(settings, source)
    included = sorted(key for key in child if sensitive(key))
    logger.info(
        "Credential isolation active; sensitive child env names: %s (values REDACTED)",
        ", ".join(included) if included else "(none)",
    )
