"""handlers/tools/restart_aliases.py — friendly /restart names → systemd units."""
from __future__ import annotations

RESTART_ALIASES: dict[str, str] = {
    "telegram": "conveyor-telegram-bot",
    "feishu": "conveyor-feishu-bot",
    "maintain": "conveyor-maintain.timer",
}

# Chinese-friendly aliases used by natural-language intent routing.
# Keep in sync with RESTART_ALIASES_ZH in handlers/tools/executors.py
# (both layers need to resolve the same surface forms to the same
# units; duplicating is intentional because executors.py cannot
# depend on the intent layer).
RESTART_ALIASES_ZH: dict[str, str] = {
    "飞书": "conveyor-feishu-bot",
    "电报": "conveyor-telegram-bot",
    "tg": "conveyor-telegram-bot",
    "维护": "conveyor-maintain.timer",
}

RESTART_USAGE = "用法: /restart telegram|feishu|maintain"


def resolve_restart_alias(arg: str) -> str | None:
    alias = (arg or "").strip().lower().split()[0] if (arg or "").strip() else ""
    if not alias:
        return None
    return RESTART_ALIASES.get(alias)
