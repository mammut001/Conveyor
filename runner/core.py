"""runner/core.py — CodexRunner class shell.

All methods live as free functions in their focused
module (runner/operators/run.py for codex spawn,
runner/worktree.py for worktree lifecycle,
runner/streaming.py for JSONL events, etc.). At import
time we attach each one to the CodexRunner class so
callers see the same public surface (runner.start,
runner.status_text, ...).
"""
from __future__ import annotations

import asyncio

from config import Settings

class CodexRunner:
    _LIFECYCLE_EVENT_TYPES = frozenset({
        "thread.started",
        "thread.completed",
        "turn.started",
        "turn.completed",
        "turn.failed",
    })
    MEMO_CATEGORIES: tuple[str, ...] = (
        "preference", "fact", "tool-quirk", "convention", "unfiled",
    )
    MEMORY_FILENAME = "MEMORY.md"
    DAILY_WORKTREE_PREFIX = "day-"
    DAILY_WORKTREE_FORMAT = "%Y-%m-%d"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock_obj: asyncio.Lock | None = None
        self.current_job: "Job | None" = None
        self.last_job: "Job | None" = None

    @property
    def _lock(self) -> asyncio.Lock:
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj


from runner.day_brief import _day_brief_state_path, _day_brief_recent_jobs, _day_brief_text

from runner.memo import _ensure_section, _extract_section, _insert_line_in_section, append_memo, read_memory, read_journal, reclassify_unfiled, classify_memo

from runner.metadata import _read_final_message, _write_job_metadata, job_records, _last_job_id, _last_worktree_path, _latest_file, _state_from_attempt_file

from runner.operators.jobs import status_text, diff_text, jobs_text, last_text, discard_last_job, apply_last_job

from runner.operators.maintain import clean_old_jobs, clean_old_worktrees

from runner.operators.run import validate, start, cancel, _run_job, _run_codex_attempt, _wait_until_cancelled, _is_rate_limited, _should_send_event_progress, _is_user_visible_event, _is_prose_event, _tool_call_name, _agent_message_text, _completed_message, _failed_message, _codex_command, _child_env, _new_job_id, _elapsed

from runner.prefetch import _tool_registry_text, _operator_profile_text, _prefetch_memory, today_memory_text, list_journal, _now_local_str

from runner.streaming import (
    _is_prose_event_text,
    _is_reasoning_event,
    _is_tool_call_start_event,
    _is_tool_call_complete_event,
    _progress_mode_allows_prose,
    _read_jsonl_stdout,
    _read_stderr,
    _capture_usage,
    _event_summary,
)

from runner.worktree import _job_worktree_path, _create_worktree, _user_today, _today_worktree_path, _memory_path, _memory_context_text, _ensure_today_worktree, _remove_worktree, _copy_untracked_files, _copy_validated_untracked_files, _git, cleanup_job_worktree

# Attach the free functions as methods via the
# descriptor protocol. setattr on the class
# turns each imported function into a real method.
for _name, _func in [
    ("_day_brief_state_path", _day_brief_state_path),
    ("_day_brief_recent_jobs", _day_brief_recent_jobs),
    ("_day_brief_text", _day_brief_text),
    ("_ensure_section", _ensure_section),
    ("_extract_section", _extract_section),
    ("_insert_line_in_section", _insert_line_in_section),
    ("append_memo", append_memo),
    ("read_memory", read_memory),
    ("read_journal", read_journal),
    ("reclassify_unfiled", reclassify_unfiled),
    ("classify_memo", classify_memo),
    ("_read_final_message", _read_final_message),
    ("_write_job_metadata", _write_job_metadata),
    ("job_records", job_records),
    ("_last_job_id", _last_job_id),
    ("_last_worktree_path", _last_worktree_path),
    ("_latest_file", _latest_file),
    ("_state_from_attempt_file", _state_from_attempt_file),
    ("status_text", status_text),
    ("diff_text", diff_text),
    ("jobs_text", jobs_text),
    ("last_text", last_text),
    ("discard_last_job", discard_last_job),
    ("apply_last_job", apply_last_job),
    ("clean_old_jobs", clean_old_jobs),
    ("clean_old_worktrees", clean_old_worktrees),
    ("validate", validate),
    ("start", start),
    ("cancel", cancel),
    ("_run_job", _run_job),
    ("_run_codex_attempt", _run_codex_attempt),
    ("_wait_until_cancelled", _wait_until_cancelled),
    ("_is_rate_limited", _is_rate_limited),
    ("_should_send_event_progress", _should_send_event_progress),
    ("_is_user_visible_event", _is_user_visible_event),
    ("_is_prose_event", _is_prose_event),
    ("_tool_call_name", _tool_call_name),
    ("_agent_message_text", _agent_message_text),
    ("_completed_message", _completed_message),
    ("_failed_message", _failed_message),
    ("_codex_command", _codex_command),
    ("_child_env", _child_env),
    ("_new_job_id", _new_job_id),
    ("_elapsed", _elapsed),
    ("_tool_registry_text", _tool_registry_text),
    ("_operator_profile_text", _operator_profile_text),
    ("_prefetch_memory", _prefetch_memory),
    ("today_memory_text", today_memory_text),
    ("list_journal", list_journal),
    ("_now_local_str", _now_local_str),
    ("_is_reasoning_event", _is_reasoning_event),
    ("_is_tool_call_start_event", _is_tool_call_start_event),
    ("_is_tool_call_complete_event", _is_tool_call_complete_event),
    ("_is_prose_event_text", _is_prose_event_text),
    ("_progress_mode_allows_prose", _progress_mode_allows_prose),
    ("_read_jsonl_stdout", _read_jsonl_stdout),
    ("_read_stderr", _read_stderr),
    ("_capture_usage", _capture_usage),
    ("_event_summary", _event_summary),
    ("_job_worktree_path", _job_worktree_path),
    ("_create_worktree", _create_worktree),
    ("_user_today", _user_today),
    ("_today_worktree_path", _today_worktree_path),
    ("_memory_path", _memory_path),
    ("_memory_context_text", _memory_context_text),
    ("_ensure_today_worktree", _ensure_today_worktree),
    ("_remove_worktree", _remove_worktree),
    ("_copy_untracked_files", _copy_untracked_files),
    ("_copy_validated_untracked_files", _copy_validated_untracked_files),
    ("_git", _git),
    ("cleanup_job_worktree", cleanup_job_worktree),
]:
    setattr(CodexRunner, _name, _func)

# Module-level aliases for the class-level constants
# so other runner/ modules can do
# `from runner.core import MEMORY_FILENAME` etc.
_LIFECYCLE_EVENT_TYPES = CodexRunner._LIFECYCLE_EVENT_TYPES
MEMO_CATEGORIES = CodexRunner.MEMO_CATEGORIES
MEMORY_FILENAME = CodexRunner.MEMORY_FILENAME
DAILY_WORKTREE_PREFIX = CodexRunner.DAILY_WORKTREE_PREFIX
DAILY_WORKTREE_FORMAT = CodexRunner.DAILY_WORKTREE_FORMAT
