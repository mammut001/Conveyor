"""Runner: lane locks, agent process groups, git timeouts and byte-exact patches."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conveyor.core import job_lanes
from runner import CodexRunner
from runner import process_group
from runner.operators import run as run_ops
from runner.operators.jobs import apply_job
from runner.types import Job, JobMode, JobState
from runner.worktree import (
    GitTimeoutError,
    _copy_validated_untracked_files,
    _create_worktree,
    _git,
    _job_worktree_path,
    _remove_worktree,
    _validate_reuse_worktree,
)
from tests.test_job_lanes import _settings as _lane_settings


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _kill_quietly(pid_file: Path) -> None:
    try:
        os.kill(int(pid_file.read_text().strip()), 9)
    except (OSError, ValueError):
        pass


def _run_in_thread(coro_factory, timeout: float) -> tuple[bool, object]:
    """Run a coroutine on its own loop in a daemon thread.

    A deadlock that blocks the event loop thread (a blocking flock) cannot be
    interrupted by asyncio.wait_for, so the bound has to come from outside.
    """
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # reported to the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return True, box.get("result")


class ParallelLaneStartTest(unittest.TestCase):
    """P0-2: start() in one lane while another lane holds run.lock across an await."""

    def test_start_does_not_block_the_loop_while_another_lane_creates_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = _lane_settings(root)

            async def scenario():
                base = CodexRunner(settings)
                lane_a = job_lanes.runner_for_lane(base, "lane-a")
                lane_b = job_lanes.runner_for_lane(base, "lane-b")
                b_inside = asyncio.Event()
                done: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}

                def wire(runner, name, hold):
                    async def validate():
                        return None

                    async def create_worktree(job):
                        if hold:
                            b_inside.set()
                            await asyncio.sleep(0.5)  # e.g. `git worktree add`
                        path = root / "wt" / job.id
                        path.mkdir(parents=True)
                        return path

                    async def attempt(job, _on_progress):
                        job.return_code = 0
                        job.final_message_path.write_text("ok", encoding="utf-8")

                    runner.validate = validate
                    runner._create_worktree = create_worktree
                    runner._run_codex_attempt = attempt

                    async def on_progress(_text):
                        done[name].set()
                    return on_progress

                progress_a = wire(lane_a, "a", hold=False)
                progress_b = wire(lane_b, "b", hold=True)
                job_b = await lane_b.start(JobMode.RUN, "b", progress_b)
                await asyncio.wait_for(b_inside.wait(), 5)
                job_a = await lane_a.start(JobMode.RUN, "a", progress_a)
                await asyncio.wait_for(asyncio.gather(done["a"].wait(), done["b"].wait()), 5)
                for _ in range(50):
                    if job_a.finished_at and job_b.finished_at:
                        break
                    await asyncio.sleep(0.02)
                return job_a.state, job_b.state

            finished, states = _run_in_thread(scenario, timeout=10)
            self.assertTrue(finished, "start() blocked the event loop on run.lock (deadlock)")
            self.assertEqual(states, (JobState.COMPLETED, JobState.COMPLETED))

    def test_every_run_lock_user_takes_the_gate(self) -> None:
        import inspect

        for func in (run_ops.start, run_ops._run_job):
            source = inspect.getsource(func)
            gate = source.index('async with async_gate("run"):')
            lock = source.index("with file_lock(lock_path):")
            self.assertLess(gate, lock, func.__name__)


def _agent_script(root: Path, *, ignore_term: bool) -> tuple[Path, Path]:
    """A fake agent that leaves a grandchild holding its stdout/stderr."""
    pid_file = root / "grandchild.pid"
    script = root / "fake-agent"
    trap = "trap '' TERM\n" if ignore_term else ""
    script.write_text(
        "#!/bin/bash\n"
        "cat >/dev/null\n"
        f"{trap}"
        f"sleep 300 &\necho $! > '{pid_file}'\n"
        "while true; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, pid_file


class AgentProcessGroupTest(unittest.TestCase):
    """P1-3: timeout and cancel stop the agent's grandchildren and finish the job."""

    def _runner(self, root: Path, script: Path, timeout: int) -> tuple[CodexRunner, Job]:
        settings = replace(_lane_settings(root), codex_bin=str(script), codex_timeout_seconds=timeout)
        (root / "tasks").mkdir(parents=True, exist_ok=True)
        runner = CodexRunner(settings)
        job = Job(
            "job-pg", JobMode.RUN, "hello", "workspace-write", state=JobState.RUNNING,
            worktree_path=root, log_path=root / "tasks" / "job.jsonl",
            final_message_path=root / "tasks" / "final.txt",
        )
        runner.current_job = job
        return runner, job

    def _assert_clean(self, pid_file: Path) -> None:
        pid = int(pid_file.read_text().strip())
        self.assertTrue(_wait_dead(pid), f"grandchild {pid} survived")

    def test_timeout_kills_the_grandchild_and_returns(self) -> None:
        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                script, pid_file = _agent_script(root, ignore_term=ignore_term)
                self.addCleanup(_kill_quietly, pid_file)
                runner, job = self._runner(root, script, timeout=1)

                async def scenario():
                    async def on_progress(_t):
                        return None
                    await run_ops._run_codex_attempt(runner, job, on_progress)
                    await asyncio.sleep(0)
                    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

                with mock.patch.object(process_group, "AGENT_TERM_GRACE_SECONDS", 0.5), \
                        mock.patch.object(process_group, "STREAM_DRAIN_SECONDS", 1.0):
                    finished, leftover = _run_in_thread(scenario, timeout=15)
                self.assertTrue(finished, "job never finished after the timeout")
                self.assertEqual(leftover, [])
                self.assertIn("Timed out", job.error)
                self.assertIsNotNone(job.return_code)
                self.assertIsNone(job.process)
                self._assert_clean(pid_file)

    def test_cancel_kills_the_grandchild_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script, pid_file = _agent_script(root, ignore_term=False)
            self.addCleanup(_kill_quietly, pid_file)
            runner, job = self._runner(root, script, timeout=60)

            async def scenario():
                async def on_progress(_t):
                    return None
                attempt = asyncio.create_task(run_ops._run_codex_attempt(runner, job, on_progress))
                for _ in range(100):
                    if pid_file.exists() and pid_file.read_text().strip():
                        break
                    await asyncio.sleep(0.05)
                message = await runner.cancel()
                await attempt
                return message

            with mock.patch.object(process_group, "STREAM_DRAIN_SECONDS", 1.0):
                finished, message = _run_in_thread(scenario, timeout=15)
            self.assertTrue(finished, "job never finished after cancel")
            self.assertIn("Cancellation requested", str(message))
            self.assertTrue(job.cancel_requested)
            self.assertIsNotNone(job.return_code)
            self._assert_clean(pid_file)


class _FakeGitProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int = 0) -> None:
        self._out, self._err, self.returncode, self.pid = stdout, stderr, returncode, None

    async def communicate(self, _input=None):
        return self._out, self._err


class GitTimeoutTest(unittest.TestCase):
    """P2-6: _git has a timeout and never returns stderr as stdout."""

    def test_hung_git_times_out_and_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pid_file = root / "git.pid"
            self.addCleanup(_kill_quietly, pid_file)
            real_exec = asyncio.create_subprocess_exec

            async def hung_git(*_cmd, **kwargs):
                return await real_exec("bash", "-c", f"sleep 300 & echo $! > '{pid_file}'; wait", **kwargs)

            runner = SimpleNamespace(settings=SimpleNamespace())

            async def scenario():
                started = time.monotonic()
                with mock.patch("asyncio.create_subprocess_exec", hung_git):
                    with self.assertRaises(GitTimeoutError):
                        await _git(runner, ["status"], cwd=root, timeout=0.5)
                return time.monotonic() - started

            finished, elapsed = _run_in_thread(scenario, timeout=15)
            self.assertTrue(finished, "_git never returned")
            self.assertLess(elapsed, 5)
            self.assertTrue(_wait_dead(int(pid_file.read_text().strip())))

    def test_warning_on_stderr_is_not_returned_as_output(self) -> None:
        async def fake_exec(*_cmd, **_kwargs):
            return _FakeGitProcess(b"", b"warning: in the working copy of 'a.txt', CRLF will be replaced by LF\n")

        runner = SimpleNamespace(settings=SimpleNamespace())
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            out = asyncio.run(_git(runner, ["diff", "--binary", "HEAD"], cwd=Path("."), check=False))
        self.assertEqual(out, "")


class _ApplyRunner:
    _git = _git
    _job_worktree_path = _job_worktree_path
    _create_worktree = _create_worktree
    _validate_reuse_worktree = _validate_reuse_worktree
    _remove_worktree = _remove_worktree
    _copy_validated_untracked_files = _copy_validated_untracked_files
    apply_job = apply_job

    def __init__(self, settings):
        self.settings = settings
        self.current_job = self.last_job = None


GBK_BASE = "第一行\n".encode("gbk")
GBK_CHANGED = GBK_BASE + "新增的中文行\n".encode("gbk") + b"latin-1: caf\xe9\n"


class ApplyBytesTest(unittest.TestCase):
    """P2-7 and P2-6: /apply keeps non-UTF-8 bytes; an empty diff is not a patch."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)  # noqa: E731
        git("init", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Conveyor Test")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "legacy.txt").write_bytes(GBK_BASE)
        git("add", "docs/legacy.txt")
        git("commit", "-m", "base")
        self.settings = SimpleNamespace(
            codex_workspace_root=self.repo.resolve(),
            codex_task_root=(root / "tasks").resolve(),
            codex_memory_root=(root / "memory").resolve(),
            conveyor_max_worktrees_bytes=1024 * 1024 * 1024,
            conveyor_apply_allow_high_risk=False,
            conveyor_apply_max_untracked_bytes=1024 * 1024,
            user_timezone="UTC",
        )
        (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)
        self.runner = _ApplyRunner(self.settings)
        job = Job("bytes-job", JobMode.FIX, "edit", "danger-full-access")
        self.worktree = asyncio.run(self.runner._create_worktree(job))
        self.addCleanup(subprocess.run, ["git", "worktree", "prune"], cwd=self.repo, capture_output=True)

    def test_gbk_file_is_applied_byte_for_byte(self) -> None:
        (self.worktree / "docs" / "legacy.txt").write_bytes(GBK_CHANGED)
        result = asyncio.run(self.runner.apply_job("bytes-job", self.worktree))
        self.assertIn("Applied bytes-job", result)
        self.assertEqual((self.repo / "docs" / "legacy.txt").read_bytes(), (self.worktree / "docs" / "legacy.txt").read_bytes())

    def test_empty_diff_with_a_git_warning_reports_no_changes(self) -> None:
        # A tracked change the path collector sees, but whose patch is empty
        # while git prints a warning (CRLF normalisation) on stderr.
        (self.worktree / "docs" / "legacy.txt").write_bytes(GBK_CHANGED)
        real_exec = asyncio.create_subprocess_exec

        async def warn_on_diff(*cmd, **kwargs):
            if cmd[:3] == ("git", "diff", "--binary"):
                return await real_exec(
                    "bash", "-c", "echo \"warning: CRLF will be replaced by LF in legacy.txt\" >&2", **kwargs)
            return await real_exec(*cmd, **kwargs)

        with mock.patch("asyncio.create_subprocess_exec", warn_on_diff):
            result = asyncio.run(self.runner.apply_job("bytes-job", self.worktree))
        self.assertEqual(result, "Job bytes-job has no changes to apply.")
        self.assertEqual((self.repo / "docs" / "legacy.txt").read_bytes(), GBK_BASE)


if __name__ == "__main__":
    unittest.main()
