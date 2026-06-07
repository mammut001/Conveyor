"""runner/ — codex subprocess spawn, worktree, streaming,
memory, CLI. Originally a 2005-line runner.py monolith;
this package splits the responsibilities so a new
chat-feel round or operator can land in the right place.

Public surface:
  CodexRunner, Job, JobMode, JobState, JobRecord,
  ProgressCallback.
"""
from config import Settings, load_settings
from runner.types import Job, JobMode, JobState, JobRecord, ProgressCallback
from runner.core import CodexRunner
# Re-export streaming-side module constants so legacy call sites
# that do `runner.THINKING_THRESHOLD_SECONDS` keep working.
from runner.streaming import (
    THINKING_INDICATOR,
    THINKING_THRESHOLD_SECONDS,
    TOOL_PULSE_THRESHOLD_SECONDS,
    TOOL_PULSE_INTERVAL_SECONDS,
)

__all__ = [
    "CodexRunner",
    "Job",
    "JobMode",
    "JobState",
    "JobRecord",
    "ProgressCallback",
]
