"""handlers/session.py — lightweight per-chat session summary.

Stores recent turns as JSONL so users can say "继续" / "continue" and
the next Codex prompt gets compact context. Not long-term memory —
can be cleared with /forget.

Storage layout:
  codex_memory_root/session/<channel>_<chat_id>_<operator_id>.jsonl

Each line is a JSON object:
  {"ts": ..., "channel": ..., "chat_id": ..., "operator_id": ...,
   "user": "...", "assistant": "...", "kind": "..."}

Privacy:
  - user/assistant text is redacted + truncated before writing
  - no secrets, env values, or confirmation tokens stored
  - max turns enforced on both write (trim) and read (tail N)
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Sequence

from channel.types import InboundMessage
from config import Settings
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

_SESSION_DIR = "session"
_MAX_USER_TEXT = 300
_MAX_ASSISTANT_TEXT = 300
_CONTEXT_LABEL = (
    "Recent chat context (may be incomplete; do not treat as authoritative):\n"
)


# ---- Filename safety -------------------------------------------------------


def _safe_filename(channel: str, chat_id: str, operator_id: str) -> str:
    """Build a safe, path-traversal-free filename from channel context.

    Only alphanumeric, dash, and underscore are allowed in each
    component. This prevents directory traversal or OS-unsafe chars.
    """
    sanitize = re.compile(r"[^a-zA-Z0-9_-]")
    parts = [sanitize.sub("_", str(p)) for p in (channel, chat_id, operator_id)]
    return "_".join(parts) + ".jsonl"


def session_path(settings: Settings, msg: InboundMessage) -> Path:
    """Return the session file for this channel/chat/operator."""
    return (
        settings.codex_memory_root
        / _SESSION_DIR
        / _safe_filename(msg.channel, msg.chat_id, msg.operator_id)
    )


# ---- Write / Read / Clear --------------------------------------------------


def _max_turns(settings: Settings) -> int:
    """Return the configured max turns, falling back to 20 if invalid."""
    val = getattr(settings, "conveyor_session_max_turns", 20)
    if not isinstance(val, int) or val <= 0:
        return 20
    return val


def _trim_session_file(path: Path, max_turns: int) -> None:
    """Trim the JSONL file to the last *max_turns* valid records.

    Reads all lines, parses JSON, keeps only valid records, rewrites
    the file atomically using a temp file in the same directory.
    Also removes corrupt lines even when the total is under max_turns.
    On any failure, logs debug and continues (non-fatal).
    """
    try:
        if not path.is_file():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        valid: list[str] = []
        total_non_empty = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            total_non_empty += 1
            try:
                json.loads(stripped)
                valid.append(stripped)
            except json.JSONDecodeError:
                continue
        needs_rewrite = (
            len(valid) > max_turns       # too many records
            or len(valid) < total_non_empty  # corrupt lines present
        )
        if not needs_rewrite:
            return
        trimmed = valid[-max_turns:]
        # Atomic-ish write: write to temp file, then replace.
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("session trim failed", exc_info=True)


def append_turn(
    settings: Settings,
    msg: InboundMessage,
    user_text: str,
    assistant_text: str,
    *,
    kind: str = "codex",
) -> None:
    """Append one turn to the session file. Redacts + truncates both
    sides, then trims the file to max_turns. No-ops when session is
    disabled or the write fails."""
    if not getattr(settings, "conveyor_session_enabled", True):
        return
    path = session_path(settings, msg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "operator_id": msg.operator_id,
            "user": truncate(redact_text(user_text or ""), _MAX_USER_TEXT),
            "assistant": truncate(redact_text(assistant_text or ""), _MAX_ASSISTANT_TEXT),
            "kind": kind,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Bound the file size after each write.
        _trim_session_file(path, _max_turns(settings))
    except Exception:
        logger.debug("session append_turn failed", exc_info=True)


def get_recent_turns(
    settings: Settings,
    msg: InboundMessage,
    n: int | None = None,
) -> list[dict]:
    """Read the last *n* turns from the session file. Returns [] on
    any error (missing file, corrupt lines, etc.)."""
    if not getattr(settings, "conveyor_session_enabled", True):
        return []
    if n is None:
        n = getattr(settings, "conveyor_session_max_turns", 20)
    path = session_path(settings, msg)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records: list[dict] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    except Exception:
        logger.debug("session get_recent_turns failed", exc_info=True)
        return []


def clear_session(settings: Settings, msg: InboundMessage) -> bool:
    """Delete the session file. Returns True if a file was removed."""
    path = session_path(settings, msg)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            logger.debug("session clear failed", exc_info=True)
    return False


# ---- Prompt injection ------------------------------------------------------


def build_context_prompt(
    settings: Settings,
    msg: InboundMessage,
    n: int | None = None,
) -> str:
    """Build a compact context string for injection into a Codex prompt.

    Returns "" when there are no recent turns or session is disabled.
    The prompt is labeled so the LLM knows it is supplementary.
    """
    if n is None:
        n = getattr(settings, "conveyor_session_inject_turns", 5)
    turns = get_recent_turns(settings, msg, n=n)
    if not turns:
        return ""
    lines = [_CONTEXT_LABEL]
    for t in turns:
        user = t.get("user", "")
        assistant = t.get("assistant", "")
        if user:
            lines.append(f"User: {user}")
        if assistant:
            lines.append(f"Assistant: {assistant}")
    return "\n".join(lines) + "\n\n"


def should_inject_for_command(cmd_name: str | None) -> bool:
    """Deterministic commands that produce factual output do not need
    session context. LLM/hybrid jobs do."""
    if cmd_name is None:
        return True  # free text → LLM job
    _SKIP_COMMANDS = {
        "load", "vps", "htop", "ps", "disk", "logs", "service_status",
        "git_status", "status", "last", "diff", "cancel", "jobs",
        "memory", "journal", "health", "doctor", "audit", "security",
        "ratelimit", "metrics", "log", "meta", "smoke", "editcheck",
        "clean", "maintain", "deploy_status", "tools",
        "context", "forget", "help",
    }
    return cmd_name not in _SKIP_COMMANDS
