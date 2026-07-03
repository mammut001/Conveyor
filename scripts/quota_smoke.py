#!/usr/bin/env python3
import asyncio
import tempfile
import time
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from config import Settings
from handlers.jobs import handle_codex_job, check_rate_limit, JOB_SUBMISSION_TIMESTAMPS
from handlers.job_queue import reset_job_queue, get_job_queue
from channel.types import InboundMessage
from runner import CodexRunner, JobState, Job
from scripts.harness_common import CheckResult, print_results

@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ph-1"

    async def send_new(self, msg, text):
        return "new-1"

    async def edit_progress(self, msg, placeholder_id, text):
        return True


def _msg(operator_id, text):
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def test_rate_limiting() -> CheckResult:
    # Set limit to 2 per hour
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=Path("/tmp"),
        codex_bin="codex",
        codex_task_root=Path("/tmp"),
        codex_model=None,
        codex_timeout_seconds=3,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=Path("/tmp"),
        user_timezone="UTC",
        conveyor_max_jobs_per_hour=2,
    )
    runner = CodexRunner(settings)
    
    # Reset submission timestamps
    JOB_SUBMISSION_TIMESTAMPS.clear()
    
    port = FakeOutbound()
    
    async def _run():
        # First 2 succeed or get processed/queued
        await handle_codex_job(_msg("op-1", "job 1"), port, runner)
        await handle_codex_job(_msg("op-1", "job 2"), port, runner)
        
        # 3rd should be rate limited
        before_len = len(port.replies)
        await handle_codex_job(_msg("op-1", "job 3"), port, runner)
        if len(port.replies) == before_len + 1 and "已达到每小时最大任务数限制" in port.replies[-1]:
            return True, "successfully rate limited"
        return False, f"replies: {port.replies}"

    ok, detail = asyncio.run(_run())
    return CheckResult("quota: hourly rate limit enforced", ok, detail)


def test_queue_limit() -> CheckResult:
    # Set limit to 1 pending job in queue
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=Path("/tmp"),
        codex_bin="codex",
        codex_task_root=Path("/tmp"),
        codex_model=None,
        codex_timeout_seconds=3,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=Path("/tmp"),
        user_timezone="UTC",
        conveyor_max_pending_jobs=1,
        conveyor_max_jobs_per_hour=100, # prevent rate limiting interference
    )
    runner = CodexRunner(settings)
    reset_job_queue()
    JOB_SUBMISSION_TIMESTAMPS.clear()
    
    # Mock a currently running job so new ones must queue
    class FakeJob:
        id = "job-running"
        state = JobState.RUNNING
    runner.current_job = FakeJob()
    
    port = FakeOutbound()
    
    async def _run():
        # 1st queued (succeeds)
        await handle_codex_job(_msg("op-1", "job 1"), port, runner)
        
        # 2nd queued (fails queue full)
        before_len = len(port.replies)
        await handle_codex_job(_msg("op-1", "job 2"), port, runner)
        if len(port.replies) == before_len + 1 and "队列已满" in port.replies[-1]:
            return True, "successfully blocked queue overflow"
        return False, f"replies: {port.replies}"

    ok, detail = asyncio.run(_run())
    return CheckResult("quota: queue capacity limit enforced", ok, detail)


def test_worktree_size_limit() -> CheckResult:
    with tempfile.TemporaryDirectory() as tmp:
        task_root = Path(tmp) / "task"
        task_root.mkdir()
        
        # Write large file to mock size
        worktrees_dir = task_root / "worktrees"
        worktrees_dir.mkdir()
        wt1 = worktrees_dir / "wt1"
        wt1.mkdir()
        
        # 200 bytes file
        (wt1 / "large.txt").write_text("a" * 200)
        
        # Set limit to 100 bytes
        settings = Settings(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=1,
            codex_workspace_root=Path(tmp),
            codex_bin="codex",
            codex_task_root=task_root,
            codex_model=None,
            codex_timeout_seconds=3,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=Path(tmp),
            user_timezone="UTC",
            conveyor_max_worktrees_bytes=100,
        )
        
        runner = CodexRunner(settings)
        job = Job(id="job-1", mode="run", prompt="test", sandbox="danger-full-access")
        
        async def _run():
            try:
                await runner._create_worktree(job)
                return False, "did not raise worktree quota error"
            except RuntimeError as exc:
                if "Worktree quota exceeded" in str(exc):
                    return True, "successfully blocked large worktree creation"
                return False, f"raised unexpected error: {exc}"
                
        ok, detail = asyncio.run(_run())
        return CheckResult("quota: worktree size limit enforced", ok, detail)


def main() -> int:
    results = [
        test_rate_limiting(),
        test_queue_limit(),
        test_worktree_size_limit(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("quota smoke ok" if ok else "quota smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
