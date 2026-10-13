"""Manage routine event hooks through chat or Telegram's normal tool approval flow."""
from __future__ import annotations

import json
import event_triggers
from personal_tools.base import ToolResult


async def hook_list(settings, arg="", **kwargs):
    if not event_triggers.enabled(settings):
        return ToolResult(False, "Hooks disabled (CONVEYOR_HOOKS_ENABLED=1).")
    rows = event_triggers.list_hooks(settings)
    if not rows:
        return ToolResult(True, "No hooks configured.")
    return ToolResult(True, "\n".join(
        f"#{r['routine_id']} {r['hook_id']} ({r['mode']}) {'paused' if r['paused'] else 'active'} "
        f"filter={json.dumps(r['filter'], ensure_ascii=False)}" for r in rows
    ))


async def hook_manage(settings, arg="", **kwargs):
    """add <routine id> [github] [| JSON filter]; rotate|pause|resume|delete <hook id>."""
    if not event_triggers.enabled(settings):
        return ToolResult(False, "Hooks disabled (CONVEYOR_HOOKS_ENABLED=1).")
    action, _, rest = (arg or "").strip().partition(" ")
    subject, _, filter_text = rest.partition("|")
    subject = subject.strip()
    try:
        if action == "add":
            tokens = subject.split()
            if not 1 <= len(tokens) <= 2 or not tokens[0].isdecimal():
                raise ValueError("expected routine ID [github]")
            if len(tokens) == 2 and tokens[1] != "github":
                raise ValueError("mode must be github or omitted (generic)")
            mode = "github" if len(tokens) == 2 else "generic"
            filt = json.loads(filter_text.strip()) if filter_text.strip() else {}
            item = event_triggers.create_hook(settings, int(tokens[0]), filt, mode,
                                              claim_in_web=True)
            return ToolResult(True, f"Hook: {item['path']} ({mode}). Secret available once in Web Console via authenticated GET /api/hooks/{item['hook_id']}/secret.")
        if action == "rotate":
            item = event_triggers.rotate_hook(settings, subject, claim_in_web=True)
            return ToolResult(True, f"Rotated hook: {item['path']}. Secret available once in Web Console via authenticated GET /api/hooks/{item['hook_id']}/secret.")
        if action in ("pause", "resume"):
            item = event_triggers.set_paused(settings, subject, action == "pause")
            return ToolResult(True, f"{item['hook_id']}: {'paused' if item['paused'] else 'active'}")
        if action == "delete":
            if not event_triggers.delete_hook(settings, subject):
                raise KeyError("hook not found")
            return ToolResult(True, "Hook deleted.")
        return ToolResult(False, "Usage: add <routine id> [github] [| JSON filter]; rotate|pause|resume|delete <hook id>")
    except (ValueError, KeyError, TypeError) as exc:
        return ToolResult(False, str(exc))
