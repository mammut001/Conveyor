"""handlers/tools/restart_aliases.py — friendly /restart names → systemd units."""
from __future__ import annotations

RESTART_ALIASES: dict[str, str] = {
    "telegram": "conveyor-telegram-bot",
    "feishu": "conveyor-feishu-bot",
    "maintain": "conveyor-maintain.timer",
}

RESTART_USAGE = "用法: /restart telegram|feishu|maintain"


def resolve_restart_alias(arg: str) -> str | None:
    alias = (arg or "").strip().lower().split()[0] if (arg or "").strip() else ""
    if not alias:
        return None
    return RESTART_ALIASES.get(alias)
