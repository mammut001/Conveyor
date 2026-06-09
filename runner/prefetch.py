"""runner/prefetch.py — split out of runner.py.

The original runner.py was 2005 lines and 5 big
responsibilities. This file is one slice.

runner/core.py attaches each function on this module
to the CodexRunner class as a method at import time,
so callers see the same public surface.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# Module-level constants (also on CodexRunner class shell)

DAILY_WORKTREE_FORMAT = "%Y-%m-%d"
from runner.types import Job
from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
def _tool_registry_text(self, job: Job) -> str:
    # Chat-first (docs/001): RUN and FIX share one workspace-write registry.
    # The bot's keyword fast path handles bare 记 x / /memo without codex.
    return (
        '<tool-registry sandbox="workspace-write" policy="fact-auto-user-explicit-otherwise">\n'
        "This is a conversational agent turn: you CAN run shell, use web tools,\n"
        "modify files in the worktree, and invoke the runner CLI. The bot's\n"
        "keyword fast path already handles bare 记 x / /memo without codex.\n\n"
        "DO NOT use codex's built-in apply_patch / edit_file / write_file tools\n"
        "to modify MEMORY.md or any other file. codex_core::tools::router rejects\n"
        "them as \"unsupported call\" in this sandbox config. For ALL writes to\n"
        "MEMORY.md or any other file, you MUST invoke `python -m runner memorize`\n"
        "(or another shell command) via the shell tool — that is the only path\n"
        "the router accepts for memory edits.\n\n"
        "Available tools (cd \"$CODEX_WORKSPACE_ROOT\" first to land in the project root):\n\n"
        "  memorize: write a single categorized entry into today's MEMORY.md.\n"
        "    The runner CLI is NOT in $CODEX_WORKSPACE_ROOT — it lives at\n"
        "    $CODEX_RUNNER_HOME (the bot's own project root). The sandbox\n"
        "    has that dir mounted as an extra writable path via --add-dir, so:\n"
        "      cd \"$CODEX_RUNNER_HOME\" && .venv/bin/python -m runner memorize [--category <cat>] [--quiet] \"<content>\"\n"
        "    Categories: fact | preference | convention | tool-quirk | unfiled\n"
        "    Omit --category to let the runner's classifier pick one. Default\n"
        '    auto-timestamp is on for "fact" only. Pass --quiet to suppress the\n'
        '    "记下了: ..." confirmation line.\n\n'
        "  memorize policy (three-tier):\n"
        "    - fact: you MAY auto-invoke when something is objectively true and\n"
        "      verifiable (e.g. a close price, a server IP, a tool's behavior).\n"
        "    - preference / convention / tool-quirk: only invoke when the user\n"
        "      EXPLICITLY asked (e.g. '记住...', '/memo ...', or said they want\n"
        "      a preference recorded). Do not infer from casual conversation.\n"
        "    - unfiled: safe landing when the category is unclear. The 12pm\n"
        "      cron will reclassify unfiled entries via the runner's classifier.\n\n"
        "  recall_memory: read today's MEMORY.md (or one section).\n"
        '    Use: python -m runner recall-memory [category]\n'
        "    Output: section markdown to stdout, empty on miss, rc=0.\n\n"
        "  recall_journal: read a past day's archived journal.\n"
        '    Use: python -m runner recall-journal <YYYY-MM-DD> [category]\n'
        "    Output: section markdown to stdout, empty on miss, rc=0.\n\n"
        "  shell: run any shell command. `cd \"$CODEX_WORKSPACE_ROOT\" && <cmd>`\n"
        "    first to land in the project root (worktree-relative paths won't\n"
        "    resolve from codex's cwd).\n\n"
        "  git_status: not a separate tool; use `git status` via shell.\n\n"
        "SECRETS: never write API keys, tokens, or passwords into MEMORY.md or\n"
        "any committed file. The runner has a redaction layer; don't rely on it.\n"
        "</tool-registry>\n\n"
    )


def _operator_profile_text(self) -> str:
    # Onboarding-A. Always-on context: who the operator is, what
    # language and tone the agent should default to. Injected at
    # the top of every prompt so the agent doesn't have to
    # re-discover the operator's identity on each session. Values
    # come from .env (OPERATOR_NAME / LANGUAGE / STYLE / STANDING);
    # defaults match the project's single-operator / zh-CN / terse
    # / personal-scale assumption (see config.py). Empty
    # OPERATOR_NAME falls back to "(anonymous)" so the name attr is
    # always non-empty for the model. The language and style are
    # duplicated inside the block (attrs + prose) so the directive
    # is harder to lose in a long context.
    # Hot-reload: read operator.json fresh on every call so
    # /profile edits take effect on the next job without a
    # bot restart. The file is ~200 bytes; reads are O(1)
    # from the page cache and the single-write _save
    # in bot.py is atomic on Linux (under PIPE_BUF). The
    # settings.operator_* fields (env / startup defaults)
    # are the fallback when operator.json is missing or a
    # field is unset in the JSON.
    from config import load_operator_profile as _load_op_live
    live = _load_op_live(self.settings.codex_memory_root)
    name = live.get("operator_name") or self.settings.operator_name or "(anonymous)"
    language = live.get("operator_language") or self.settings.operator_language
    style = live.get("operator_style") or self.settings.operator_style
    standing = live.get("operator_standing") or self.settings.operator_standing
    return (
        f'<operator-profile name="{name}" '
        f'language="{language}" '
        f'style="{style}" '
        f'standing="{standing}">\n'
        f"You are the operator's persistent coding agent. The "
        f"operator is a single human in the "
        f"{self.settings.user_timezone} timezone. Default reply "
        f"language: {language}. Default "
        f"tone: {style}. Setup: "
        f"{standing}.\n"
        f"</operator-profile>\n\n"
    )


def _prefetch_memory(self, job: Job) -> str:
    return (
        self._operator_profile_text()
        + self._day_brief_text()
        + self._memory_context_text(job)
        + self._tool_registry_text(job)
    )


def today_memory_text(self) -> str:
    path = self._memory_path(self._today_worktree_path())
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def list_journal(self, limit: int = 10) -> list[Path]:
    journal_dir = self.settings.codex_memory_root / "JOURNAL"
    if not journal_dir.exists():
        return []
    return sorted(journal_dir.glob("*.md"), reverse=True)[:limit]


def _now_local_str(self) -> str:
    try:
        tz = ZoneInfo(self.settings.user_timezone)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")
