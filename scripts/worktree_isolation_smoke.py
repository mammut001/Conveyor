#!/usr/bin/env python3
"""worktree_isolation_smoke.py — env-free unit tests for per-job worktrees.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results
from runner import CodexRunner
from runner.types import Job, JobMode

def _fake_settings(tmp: Path):
    return SimpleNamespace(
        codex_workspace_root=tmp / "workspace",
        codex_task_root=tmp / "conveyor",
        codex_memory_root=tmp / "memory",
        codex_model="test-model",
        codex_timeout_seconds=30,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(30,),
        user_timezone="America/Toronto",
    )

def test_worktree_isolation() -> CheckResult:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        settings = _fake_settings(tmp)
        runner = CodexRunner(settings)
        
        job1 = Job(id="job-abc-123", mode=JobMode.RUN, prompt="prompt 1", sandbox="danger-full-access")
        job2 = Job(id="job-xyz-789", mode=JobMode.RUN, prompt="prompt 2", sandbox="danger-full-access")
        
        path1 = runner._job_worktree_path(job1)
        path2 = runner._job_worktree_path(job2)
        
        ok1 = path1 != path2
        ok2 = job1.id in str(path1) and job2.id in str(path2)
        ok3 = "worktrees" in str(path1)
        
        # Verify daily worktree is different
        daily_path = runner._today_worktree_path()
        ok4 = path1 != daily_path
        
        ok = ok1 and ok2 and ok3 and ok4
        detail = f"path1={path1}, path2={path2}, daily={daily_path}"
        return CheckResult("worktree_isolation", ok, detail)

def main() -> int:
    results = [test_worktree_isolation()]
    print_results(results)
    ok = all(r.ok for r in results)
    print("worktree isolation smoke ok" if ok else "worktree isolation smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
