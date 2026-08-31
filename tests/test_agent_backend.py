from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runner.backend import AgentBackend, CodexBackend, backend_name
from runner.streaming import _read_stderr
from runner.types import Job, JobMode


class FakeRunner:
    def __init__(self) -> None:
        self.current_job = None
        self.settings = SimpleNamespace()
        self.calls: list[str] = []

    async def validate(self): self.calls.append("validate")
    async def start(self, mode, prompt, on_progress): self.calls.append(f"start:{mode.value}:{prompt}"); return "job"
    async def cancel(self): self.calls.append("cancel"); return "cancelled"
    async def diff_job(self, job_id, worktree=None): return f"diff:{job_id}"
    async def apply_job(self, job_id, worktree=None): return f"apply:{job_id}"
    async def discard_job(self, job_id, worktree=None): return f"discard:{job_id}"


class AgentBackendTests(unittest.TestCase):
    def test_codex_adapter_satisfies_protocol(self) -> None:
        backend = CodexBackend(FakeRunner())
        self.assertIsInstance(backend, AgentBackend)
        self.assertEqual(backend_name(backend), "codex")

    def test_adapter_delegates_without_behavior_change(self) -> None:
        runner = FakeRunner(); backend = CodexBackend(runner)
        async def scenario():
            async def progress(_text: str): pass
            await backend.validate()
            self.assertEqual(await backend.start(JobMode.RUN, "hello", progress), "job")
            self.assertEqual(await backend.cancel(), "cancelled")
            self.assertEqual(await backend.diff_job("q1"), "diff:q1")
            self.assertEqual(await backend.apply_job("q1"), "apply:q1")
            self.assertEqual(await backend.discard_job("q1"), "discard:q1")
        asyncio.run(scenario())
        self.assertIs(backend.settings, runner.settings)
        self.assertIs(backend.current_job, runner.current_job)
        self.assertIn("validate", runner.calls)
        self.assertIn("start:run:hello", runner.calls)
        self.assertIn("cancel", runner.calls)

    def test_successful_provider_warning_on_stderr_is_not_a_job_error(self) -> None:
        class Reader:
            def __init__(self) -> None:
                self.lines = [b"websocket unavailable; using HTTP fallback\n", b""]

            async def readline(self) -> bytes:
                return self.lines.pop(0)

        class Process:
            stderr = Reader()

            async def wait(self) -> int:
                return 0

        with tempfile.TemporaryDirectory() as temp:
            job = Job("job-1", JobMode.RUN, "read", "unused")
            # Exercise the race where stderr reaches EOF just before the main
            # waiter stores the successful return code on the job.
            job.return_code = None
            job.final_message_path = Path(temp) / "final.txt"
            job.final_message_path.write_text("done", encoding="utf-8")
            process = Process()
            asyncio.run(_read_stderr(None, job, process))
            self.assertEqual(job.error, "")


if __name__ == "__main__":
    unittest.main()
