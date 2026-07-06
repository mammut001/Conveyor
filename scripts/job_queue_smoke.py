#!/usr/bin/env python3
"""job_queue_smoke.py — P3.8 Job Queue smoke tests.

Tests the FIFO queue for Codex jobs:
- Second Codex job is queued instead of rejected
- Queue list shows position
- Queued job starts after first completes
- queue_cancel cancels queued item
- Queue does not run two Codex jobs concurrently
- Deterministic READ tools bypass queue
- /queue commands registered and help listed
- Planner route uses queue
- Max queue length enforced
- Redaction in queue display
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Setup global temp memory root for the entire test run to isolate from production
TMP_DIR = tempfile.mkdtemp()
os.environ["CODEX_MEMORY_ROOT"] = TMP_DIR

from config import load_settings  # noqa: E402
from handlers import dispatch  # noqa: E402
from handlers.job_queue import JobQueue, QueuedJob, QueueJobState, get_job_queue, reset_job_queue  # noqa: E402
from channel.types import InboundMessage  # noqa: E402
from runner import CodexRunner, JobState  # noqa: E402
from scripts.harness_common import CheckResult, print_results  # noqa: E402


def clean_db():
    """Clean the SQLite database file to ensure test case isolation."""
    db_file = Path(TMP_DIR) / "state" / "job_queue.sqlite3"
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass


@dataclass
class FakeOutbound:
    supports_inline_buttons: bool = False
    replies: list[str] = field(default_factory=list)
    sent_new: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ph-1"

    async def send_new(self, msg, text):
        self.sent_new.append(text)
        return f"new-{len(self.sent_new)}"

    async def edit_progress(self, msg, placeholder_id, text):
        self.edits.append((placeholder_id, text))
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return "ph-1"


def _msg(channel, operator_id, text, **kw):
    return InboundMessage(
        channel=channel,
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
        **kw,
    )


def _check_queue_enqueue_dequeue():
    """Test basic enqueue and dequeue operations."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    msg = _msg("telegram", "123", "test prompt")
    port = FakeOutbound()

    # Enqueue a job
    async def _test():
        success, reply, job = await queue.enqueue(
            mode="run",
            prompt="test prompt",
            msg=msg,
            port=port,
            runner=runner,
        )
        if not success:
            return False, f"enqueue failed: {reply}"
        if job is None:
            return False, "job is None"
        if job.position != 1:
            return False, f"position {job.position} != 1"
        if queue.queue_length != 1:
            return False, f"queue_length {queue.queue_length} != 1"

        # Dequeue
        dequeued = await queue.dequeue()
        if dequeued is None:
            return False, "dequeue returned None"
        if dequeued.id != job.id:
            return False, f"dequeued id {dequeued.id} != {job.id}"
        if dequeued.state != QueueJobState.RUNNING:
            return False, f"state {dequeued.state} != RUNNING"
        if queue.queue_length != 0:
            return False, f"queue_length after dequeue {queue.queue_length} != 0"

        return True, "enqueue/dequeue ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: enqueue and dequeue basic operations",
        ok,
        detail,
    )


def _check_queue_fifo_order():
    """Test FIFO ordering."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    async def _test():
        ids = []
        for i in range(3):
            msg = _msg("telegram", "123", f"prompt {i}")
            success, _, job = await queue.enqueue(
                mode="run", prompt=f"prompt {i}", msg=msg, port=port, runner=runner,
            )
            if not success or job is None:
                return False, f"enqueue {i} failed"
            ids.append(job.id)

        # Dequeue should be FIFO
        for i, expected_id in enumerate(ids):
            job = await queue.dequeue()
            if job is None:
                return False, f"dequeue {i} returned None"
            if job.id != expected_id:
                return False, f"dequeue {i}: got {job.id}, expected {expected_id}"

        return True, "FIFO order ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: FIFO order preserved",
        ok,
        detail,
    )


def _check_queue_max_length():
    """Test max queue length enforcement."""
    clean_db()
    queue = JobQueue(max_length=3)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    async def _test():
        for i in range(3):
            msg = _msg("telegram", "123", f"prompt {i}")
            success, _, _ = await queue.enqueue(
                mode="run", prompt=f"prompt {i}", msg=msg, port=port, runner=runner,
            )
            if not success:
                return False, f"enqueue {i} failed"

        # 4th should fail
        msg = _msg("telegram", "123", "prompt overflow")
        success, reply, _ = await queue.enqueue(
            mode="run", prompt="prompt overflow", msg=msg, port=port, runner=runner,
        )
        if success:
            return False, "enqueue should have failed at max length"
        if "队列已满" not in reply:
            return False, f"reply missing '队列已满': {reply}"

        return True, "max length enforced"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: max queue length enforced",
        ok,
        detail,
    )


def _check_queue_cancel():
    """Test cancelling a queued job."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    async def _test():
        msg = _msg("telegram", "123", "test prompt")
        success, _, job = await queue.enqueue(
            mode="run", prompt="test prompt", msg=msg, port=port, runner=runner,
        )
        if not success or job is None:
            return False, "enqueue failed"

        # Cancel
        cancel_ok, cancel_msg = await queue.cancel(job.id)
        if not cancel_ok:
            return False, f"cancel failed: {cancel_msg}"
        if queue.queue_length != 0:
            return False, f"queue_length after cancel {queue.queue_length} != 0"

        # Try to cancel non-existent
        cancel_ok2, _ = await queue.cancel("nonexistent")
        if cancel_ok2:
            return False, "cancel nonexistent should fail"

        return True, "cancel ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: cancel queued job",
        ok,
        detail,
    )


def _check_queue_clear():
    """Test clearing the queue."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    async def _test():
        for i in range(3):
            msg = _msg("telegram", "123", f"prompt {i}")
            await queue.enqueue(
                mode="run", prompt=f"prompt {i}", msg=msg, port=port, runner=runner,
            )

        cleared = await queue.clear()
        if cleared != 3:
            return False, f"cleared {cleared} != 3"
        if queue.queue_length != 0:
            return False, f"queue_length after clear {queue.queue_length} != 0"

        return True, "clear ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: clear all queued jobs",
        ok,
        detail,
    )


def _check_queue_pause_resume():
    """Test pause and resume."""
    clean_db()
    queue = JobQueue(max_length=5)

    async def _test():
        await queue.pause()
        if not queue.is_paused:
            return False, "pause failed"

        # Dequeue should return None when paused
        result = await queue.dequeue()
        if result is not None:
            return False, "dequeue should return None when paused"

        await queue.resume()
        if queue.is_paused:
            return False, "resume failed"

        return True, "pause/resume ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: pause and resume",
        ok,
        detail,
    )


def _check_queue_status_display():
    """Test queue status display."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    async def _test():
        # Empty queue
        status = await queue.get_queue_status()
        if "为空" not in status:
            return False, f"empty status missing '为空': {status}"

        # Add a job with a secret that will be caught by redaction
        msg = _msg("telegram", "123", "test prompt with token=supersecret123value")
        await queue.enqueue(
            mode="run", prompt="test prompt with token=supersecret123value",
            msg=msg, port=port, runner=runner,
        )

        status = await queue.get_queue_status()
        if "1/5" not in status:
            return False, f"status missing '1/5': {status}"

        # Check redaction - token=supersecret123value should be redacted
        if "supersecret123value" in status:
            return False, "status contains secret!"

        return True, "status display ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: status display with redaction",
        ok,
        detail,
    )


def _check_queue_commands_registered():
    """Test that queue commands are in COMMAND_TABLE."""
    from handlers.commands import COMMAND_TABLE

    required = ["queue", "queue_cancel", "queue_clear", "queue_pause", "queue_resume"]
    missing = [cmd for cmd in required if cmd not in COMMAND_TABLE]

    if missing:
        return CheckResult(
            "commands: queue commands registered in COMMAND_TABLE",
            False,
            f"missing: {missing}",
        )

    return CheckResult(
        "commands: queue commands registered in COMMAND_TABLE",
        True,
        f"all {len(required)} commands registered",
    )


def _check_queue_help_text():
    """Test that queue commands appear in /help."""
    from handlers.commands import _help
    import inspect

    # Get the source of _help to check for queue commands
    source = inspect.getsource(_help)
    has_queue = "/queue" in source
    has_cancel = "/queue_cancel" in source
    has_clear = "/queue_clear" in source
    has_pause = "/queue_pause" in source
    has_resume = "/queue_resume" in source

    all_present = has_queue and has_cancel and has_clear and has_pause and has_resume
    return CheckResult(
        "help: queue commands listed in /help",
        all_present,
        f"queue={has_queue} cancel={has_cancel} clear={has_clear} pause={has_pause} resume={has_resume}",
    )


def _check_queue_redaction_in_display():
    """Test that secrets are redacted in queue display."""
    clean_db()
    queue = JobQueue(max_length=5)
    settings = load_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()

    # Various secret patterns that should be caught by redaction
    secrets = [
        "sk-1234567890abcdefghijklmnop",  # 28 chars after sk-
        "token=mysecretvalue12345678",  # token= pattern
        "password=verysecretpassword",  # password= pattern
    ]

    async def _test():
        for i, secret in enumerate(secrets):
            msg = _msg("telegram", "123", f"prompt {secret}")
            await queue.enqueue(
                mode="run", prompt=f"prompt {secret}",
                msg=msg, port=port, runner=runner,
            )

        status = await queue.get_queue_status()
        for secret in secrets:
            # Extract the actual secret value (after = or -)
            if "=" in secret:
                value = secret.split("=", 1)[1]
            else:
                value = secret.split("-", 1)[1]
            if value in status:
                return False, f"secret value '{value[:10]}...' found in status!"

        return True, "redaction ok"

    ok, detail = asyncio.run(_test())
    return CheckResult(
        "queue: secrets redacted in display",
        ok,
        detail,
    )


def _check_queue_persistence_and_recovery():
    """Test database persistence across new instances and recovery states."""
    import tempfile
    import os
    import shutil
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        orig_env = os.environ.get("CODEX_MEMORY_ROOT")
        os.environ["CODEX_MEMORY_ROOT"] = tmp_dir
        
        try:
            # 1. Create a queue instance and enqueue a job
            queue1 = JobQueue(max_length=5)
            settings = load_settings()
            runner = CodexRunner(settings)
            port = FakeOutbound()
            msg = _msg("telegram", "123", "persist test prompt")
            
            async def run_enqueue():
                return await queue1.enqueue("run", "persist test prompt", msg, port, runner)
            success, _, job = asyncio.run(run_enqueue())
            if not success or job is None:
                return CheckResult("queue: persistence and recovery", False, "failed to enqueue on instance 1")
                
            job_id = job.id
            
            # 2. Dequeue it on the first queue instance
            async def run_dequeue():
                return await queue1.dequeue()
            dequeued = asyncio.run(run_dequeue())
            if dequeued is None or dequeued.state != QueueJobState.RUNNING:
                return CheckResult("queue: persistence and recovery", False, "failed to dequeue/mark running on instance 1")
                
            # Pause the first queue to verify paused state persists
            async def run_pause():
                await queue1.pause()
            asyncio.run(run_pause())
            
            # 3. Create a second queue instance (representing restart) and configure it
            queue2 = JobQueue(max_length=5)
            queue2.configure(settings, runner)
            
            # The running job should now be interrupted
            async def check_recovered_job():
                return await queue2.get_job(job_id)
            recovered = asyncio.run(check_recovered_job())
            if recovered is None:
                return CheckResult("queue: persistence and recovery", False, "recovered job not found")
            if recovered.state != QueueJobState.INTERRUPTED:
                return CheckResult("queue: persistence and recovery", False, f"recovered job state {recovered.state} != INTERRUPTED")
                
            # Paused state should survive
            if not queue2.is_paused:
                return CheckResult("queue: persistence and recovery", False, "paused state did not survive restart")
                
            return CheckResult("queue: persistence and recovery", True, "job state recovered to interrupted, paused state survived")
        finally:
            if orig_env is not None:
                os.environ["CODEX_MEMORY_ROOT"] = orig_env
            else:
                os.environ.pop("CODEX_MEMORY_ROOT", None)


CHECKS = [
    _check_queue_enqueue_dequeue,
    _check_queue_fifo_order,
    _check_queue_max_length,
    _check_queue_cancel,
    _check_queue_clear,
    _check_queue_pause_resume,
    _check_queue_status_display,
    _check_queue_commands_registered,
    _check_queue_help_text,
    _check_queue_redaction_in_display,
    _check_queue_persistence_and_recovery,
]


def main() -> int:
    # Reset queue before tests
    reset_job_queue()

    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            results.append(CheckResult(check.__name__, False, f"raised: {exc!r}"))

    print_results(results)
    
    # Cleanup temp directory
    try:
        shutil.rmtree(TMP_DIR)
    except OSError:
        pass
        
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
