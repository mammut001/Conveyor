"""handlers/ — channel-agnostic message handling.

Public surface:
  dispatch                — main entry point
  detect_memory_intent    — text → memo
  parse_command           — text → (cmd, arg)
  COMMAND_TABLE           — for adapter-side /set_my_commands equivalent
  handle_memo             — memo fast path
  handle_codex_job        — Codex job + progress
"""
from handlers.dispatch import dispatch
from handlers.memo import detect_memory_intent, handle_memo
from handlers.jobs import handle_codex_job
from handlers.commands import COMMAND_TABLE, parse_command, run_command

__all__ = [
    "dispatch",
    "detect_memory_intent",
    "handle_memo",
    "handle_codex_job",
    "parse_command",
    "run_command",
    "COMMAND_TABLE",
]
