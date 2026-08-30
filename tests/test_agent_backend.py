from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from runner.backend import AgentBackend, CodexBackend, backend_name
from runner.types import JobMode


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
            self.assertEqual(await backend.start(JobMode.RUN, "hello", progress), "job")
            self.assertEqual(await backend.cancel(), "cancelled")
            self.assertEqual(await backend.diff_job("q1"), "diff:q1")
        asyncio.run(scenario())
        self.assertIn("start:run:hello", runner.calls)
        self.assertIn("cancel", runner.calls)


if __name__ == "__main__":
    unittest.main()
