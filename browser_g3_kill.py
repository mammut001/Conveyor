"""Consistent browser kill-switch checks for all human approval channels."""
from __future__ import annotations
import os
from pathlib import Path

from personal_tools.browser_read import _db
from workflow_browser_external_identity import ExternalPolicyDenied


def path(settings) -> Path:
    configured = os.environ.get("CONVEYOR_BROWSER_EXTERNAL_KILL_FILE")
    return (Path(configured).expanduser().absolute()
            if configured else _db(settings).parent / "browser-external.kill")


def active(settings) -> bool:
    target = path(settings)
    if target.is_symlink():
        raise ExternalPolicyDenied("kill_path_unsafe")
    return target.exists()


def refusal(settings, tool_name: str) -> str | None:
    if not tool_name.startswith("browser."):
        return None
    try:
        if active(settings):
            return "Browser approval refused: external browser kill switch is ON; disable it before approving."
    except (ExternalPolicyDenied, OSError):
        return "Browser approval refused: kill switch state is unsafe or unreadable."
    return None
