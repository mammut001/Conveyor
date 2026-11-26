"""Exercise dispatch and cancellation using a real subprocess and Git worktree."""
import asyncio
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_supervisor as supervisor
from supervisor_runtime import SupervisorRuntime
from web_console import WebConsoleHandler, WebConsoleServer


class SupervisorRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "file.txt").write_text("original")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.worktree = self.root / "worktree"
        self.git("worktree", "add", "-qb", "test-task", str(self.worktree))
        self.marker = self.root / "started"
        self.binary = self.root / "executor"
        self.binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time
prompt = sys.stdin.read()
if os.environ.get("TEST_WAIT"):
    pathlib.Path(os.environ["TEST_MARKER"]).write_text(str(os.getpid()))
    time.sleep(60)
if "You are the implementer" in prompt:
    pathlib.Path("result.txt").write_text("done")
text = "PASS" if "independent reviewer" in prompt else "done"
print(json.dumps({"result": text, "is_error": False}))
''')
        self.binary.chmod(0o755)
        self.env = patch.dict(os.environ, {"CONVEYOR_CLAUDE_BIN": str(self.binary),
                                           "TEST_MARKER": str(self.marker)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = SimpleNamespace(codex_memory_root=self.root / "memory", codex_model=None,
                                        agent_supervisor_enabled=True, codex_task_root=self.root / "tasks")
        self.runtime = SupervisorRuntime(self.settings)

    async def asyncTearDown(self):
        await self.runtime.shutdown()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def task(self):
        return supervisor.create_task(self.settings, goal="add result", repo_path=str(self.repo),
                                      metadata={"validation_commands": [[sys.executable, "-c",
                                                "from pathlib import Path; assert Path('result.txt').read_text() == 'done'"]]})

    async def test_dispatch_runs_executor_and_records_snapshot(self):
        task = self.task()
        await self.runtime.dispatch(task["id"], str(self.worktree), "result.txt exists")
        worker = self.runtime.running.get(task["id"])
        if worker:
            await worker
        task = supervisor.get_task(self.settings, task["id"])
        self.assertEqual(task["state"], "completed", task["error"])
        self.assertEqual((self.worktree / "result.txt").read_text(), "done")
        self.assertTrue(supervisor.effective_acceptance(self.settings, task["id"])["passed"])
        self.assertTrue(any(h["from_role"] == "validator" and "Exit: 0" in h["summary"]
                            for h in supervisor.list_handoffs(self.settings, task["id"])))

    async def test_dispatch_can_create_isolated_worktree(self):
        (self.repo / "file.txt").write_text("local unsaved change")
        task = self.task()
        await self.runtime.dispatch(task["id"])
        worker = self.runtime.running.get(task["id"])
        if worker:
            await worker
        result = supervisor.get_task(self.settings, task["id"])
        self.assertEqual(result["state"], "completed", result["error"])
        checkout = Path(result["worktree_path"])
        self.assertEqual((checkout / "file.txt").read_text(), "original")
        self.assertEqual((self.repo / "file.txt").read_text(), "local unsaved change")
        self.assertTrue((checkout / "result.txt").exists())

    async def test_discovers_and_runs_a_project_test(self):
        tests = self.repo / "tests"
        tests.mkdir()
        (tests / "test_result.py").write_text('''import unittest
from pathlib import Path
class ResultTest(unittest.TestCase):
    def test_result(self):
        self.assertEqual(Path("result.txt").read_text(), "done")
''')
        self.git("add", "tests")
        self.git("commit", "-qm", "add project test")
        task = supervisor.create_task(self.settings, goal="add result", repo_path=str(self.repo))
        await self.runtime.dispatch(task["id"])
        worker = self.runtime.running.get(task["id"])
        if worker:
            await worker
        result = supervisor.get_task(self.settings, task["id"])
        self.assertEqual(result["state"], "completed", result["error"])
        handoffs = supervisor.list_handoffs(self.settings, task["id"])
        self.assertTrue(any("unittest tests directory" in h["summary"] for h in handoffs))
        self.assertTrue(any("Ran 1 test" in h["summary"] for h in handoffs))

    async def test_unknown_test_command_keeps_worktree_for_retry(self):
        task = supervisor.create_task(self.settings, goal="add result", repo_path=str(self.repo))
        with self.assertRaisesRegex(supervisor.SupervisorStateError, "could not determine"):
            await self.runtime.dispatch(task["id"])
        pending = supervisor.get_task(self.settings, task["id"])
        self.assertEqual(pending["state"], "pending_dispatch")
        self.assertTrue(Path(pending["worktree_path"]).is_dir())
        await self.runtime.dispatch(task["id"], validation_commands=[[sys.executable, "-c", "assert True"]])
        worker = self.runtime.running.get(task["id"])
        if worker:
            await worker
        self.assertEqual(supervisor.get_task(self.settings, task["id"])["state"], "completed")

    async def test_failing_validation_cannot_be_marked_complete(self):
        task = self.task()
        await self.runtime.dispatch(task["id"], str(self.worktree),
                                    validation_commands=[[sys.executable, "-c", "raise SystemExit(4)"]])
        worker = self.runtime.running.get(task["id"])
        if worker:
            await worker
        task = supervisor.get_task(self.settings, task["id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["repair_round"], 3)
        self.assertIsNone(supervisor.effective_acceptance(self.settings, task["id"]))
        self.assertTrue(any("Exit: 4" in h["summary"] for h in supervisor.list_handoffs(self.settings, task["id"])))

    async def test_cancel_reaps_executor_before_workspace_is_unlocked(self):
        os.environ["TEST_WAIT"] = "1"
        self.addCleanup(os.environ.pop, "TEST_WAIT", None)
        task = self.task()
        await self.runtime.dispatch(task["id"], str(self.worktree))
        for _ in range(200):
            if self.marker.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.marker.exists())
        pid = int(self.marker.read_text())
        other = self.task()
        with self.assertRaises(supervisor.SupervisorLockError):
            await self.runtime.dispatch(other["id"], str(self.worktree))
        result = await self.runtime.cancel(task["id"])
        self.assertEqual(result["state"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        supervisor.acquire_worktree_lock(self.settings, str(self.worktree), other["id"])

    async def test_rejects_nonisolated_or_dirty_checkout(self):
        task = self.task()
        with self.assertRaises(supervisor.SupervisorStateError):
            await self.runtime.dispatch(task["id"], str(self.repo))
        (self.worktree / "file.txt").write_text("unsaved work")
        with self.assertRaises(supervisor.SupervisorStateError):
            await self.runtime.dispatch(task["id"], str(self.worktree))
        self.assertEqual(supervisor.get_task(self.settings, task["id"])["state"], "pending_dispatch")

    async def test_http_dispatch_runs_the_supervisor(self):
        token = "supervisor-test-token"
        server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler,
                                  control=SimpleNamespace(settings=self.settings),
                                  loop=asyncio.get_running_loop(), token=token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json", "Authorization": "Bearer " + token})
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        try:
            status, task = await asyncio.to_thread(request, "POST", "/api/supervisor/tasks", {
                "goal": "add result", "repo_path": str(self.repo),
                "metadata": {"validation_commands": [[sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').is_file()"]]},
            })
            self.assertEqual(status, 201)
            task_id = task["id"]
            status, _ = await asyncio.to_thread(request, "POST", f"/api/supervisor/tasks/{task_id}/dispatch",
                                                {"worktree_path": str(self.worktree)})
            self.assertEqual(status, 202)
            worker = server.supervisor_runtime.running.get(task_id)
            if worker:
                await worker
            status, result = await asyncio.to_thread(request, "GET", f"/api/supervisor/tasks/{task_id}")
            self.assertEqual(status, 200)
            self.assertEqual(result["state"], "completed", result.get("error"))
            self.assertTrue(result["acceptance"]["passed"])
        finally:
            await server.supervisor_runtime.shutdown()
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)

    def test_default_executor_applies_to_all_execution_roles(self):
        self.settings.agent_supervisor_implementation_executor = "example"
        roles = supervisor.load_roles(self.settings)
        self.assertEqual([roles[name].executor for name in ("analyst", "implementer", "reviewer")],
                         ["example", "example", "example"])


if __name__ == "__main__":
    unittest.main()
