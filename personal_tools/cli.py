"""Run a low-risk personal tool from the command line.

    python -m personal_tools.cli reminders.create "明天早上8点 开会"
    python -m personal_tools.cli reminders.list
    python -m personal_tools.cli --list

Only an explicit allowlist runs here: creating, listing and showing due
reminders. Those never need the owner's confirmation in chat, so this entry
point skips no confirmation step; everything else (cancelling a reminder,
email, GitHub, browser, nightly runs, ...) is refused. Tools act for the owner
(TELEGRAM_ALLOWED_USER_ID), the same identity the web console uses.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from handlers.tools.registry import DangerLevel

ALLOWED_TOOLS = ("reminders.create", "reminders.list", "reminders.due")
# Belt and braces: an allowlisted tool must also be one that needs no confirmation.
ALLOWED_DANGER = frozenset({DangerLevel.READ, DangerLevel.WRITE_SAFE})


def allowed_tools() -> list[tuple[str, str]]:
    from personal_tools import registry

    registry.register_personal_tools()
    rows = []
    for name in ALLOWED_TOOLS:
        spec = registry.get_personal_tool(name)
        if spec is not None and spec.danger in ALLOWED_DANGER:
            rows.append((name, spec.summary))
    return rows


def run(tool: str, arg: str, settings=None) -> tuple[int, str]:
    from personal_tools import registry

    registry.register_personal_tools()
    spec = registry.get_personal_tool(tool)
    if spec is None:
        return 2, f"unknown tool: {tool} (see --list)"
    if tool not in ALLOWED_TOOLS or spec.danger not in ALLOWED_DANGER:
        return 3, f"{tool} cannot run from the command line (allowed: {', '.join(ALLOWED_TOOLS)})"
    if settings is None:
        from conveyor.core.config import load_settings

        settings = load_settings()
    owner = getattr(settings, "telegram_allowed_user_id", None)
    if not owner:
        return 4, "TELEGRAM_ALLOWED_USER_ID is not set; personal tools have no owner"
    text = asyncio.run(registry.execute_personal_tool(settings, tool, arg, operator_id=str(owner)))
    return 0, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m personal_tools.cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("tool", nargs="?", help="tool name, e.g. reminders.create")
    parser.add_argument("arg", nargs="*", help="tool argument (joined with spaces)")
    parser.add_argument("--list", action="store_true", help="list the tools this entry point can run")
    ns = parser.parse_args(argv)
    if ns.list or not ns.tool:
        for name, summary in allowed_tools():
            print(f"{name}\t{summary}")
        return 0
    code, text = run(ns.tool, " ".join(ns.arg))
    print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
