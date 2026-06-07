"""Back-compat shim.

The runner was a 2005-line monolith. It now lives in the
runner/ package. This file re-exports the public surface so
old imports (`from runner import CodexRunner, JobMode`) keep
working. New code should import from runner.types,
runner.core, runner.worktree, etc. directly.
"""
from runner import CodexRunner, Job, JobMode, JobState, JobRecord, ProgressCallback
from runner.cli import main

__all__ = [
    "CodexRunner", "Job", "JobMode", "JobState", "JobRecord",
    "ProgressCallback", "main",
]

if __name__ == "__main__":
    import sys
    sys.exit(main())
