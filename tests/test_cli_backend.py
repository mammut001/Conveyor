from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.core.config import Settings
from runner.cli_backend import CLI_CONFIGS, CliBackend
from runner.pooled import create_worker_backend
from runner.types import Job, JobMode


def _settings(root: Path, timeout: int = 10, unsandboxed: bool = True) -> Settings:
    root = root.resolve()
    state_root = root.parent / f".{root.name}-conveyor-test"
    return Settings(
        telegram_bot_token="unused",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=state_root / "tasks",
        codex_model=None,
        codex_timeout_seconds=timeout,
        telegram_progress_seconds=1,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=state_root / "memory",
        user_timezone="UTC",
        workers_unsandboxed=unsandboxed,
    )


def _job(root: Path, mode: JobMode = JobMode.FIX) -> Job:
    job = Job("job-1", mode, "do the thing", "unused")
    job.worktree_path = root
    job.log_path = root / "attempt.log"
    job.final_message_path = root / "final.txt"
    return job


def _fake_cli(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class CommandTests(unittest.TestCase):
    def _backend(self, worker: str, root: Path) -> CliBackend:
        backend = create_worker_backend(worker, _settings(root))
        self.assertIsInstance(backend, CliBackend)
        return backend

    def test_agy_command_default_and_chosen_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = self._backend("agy", root)
            job = _job(root)
            self.assertEqual(backend._cli_command(job, "P"), [
                "agy", "-p", "P", "--model", "gemini-3.8-flash-high",
                "--mode", "accept-edits", "--sandbox", "--output-format", "text"])
            backend.cli_model = "gemini-x"
            self.assertEqual(backend._cli_command(job, "P")[4], "gemini-x")

    def test_grok_command_with_and_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = self._backend("grok", root)
            job = _job(root)
            self.assertEqual(backend._cli_command(job, "P"), [
                "grok", "-p", "P", "--always-approve", "--cwd", str(root),
                "--output-format", "plain"])
            backend.cli_model = "grok-4"
            self.assertEqual(backend._cli_command(job, "P")[-2:], ["-m", "grok-4"])

    def test_cmd_command_default_and_chosen_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = self._backend("cmd", root)
            job = _job(root)
            self.assertEqual(backend._cli_command(job, "P"), [
                "cmd", "-p", "P", "--yolo", "-t", "--skip-onboarding",
                "-m", "deepseek/deepseek-v4-flash", "--max-turns", "300"])
            backend.cli_model = "other/model"
            self.assertIn("other/model", backend._cli_command(job, "P"))

    def test_model_comes_from_workers_state(self) -> None:
        from conveyor.agents import workers
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend = self._backend("cmd", root)
            with patch.object(workers, "load_workers_state",
                              return_value={"models": {"cmd": "chosen/one"}}):
                self.assertIn("chosen/one", backend._cli_command(_job(root), "P"))

    def test_factory_knows_all_three_and_rejects_unknown(self) -> None:
        self.assertEqual(set(CLI_CONFIGS), {"agy", "grok", "cmd"})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                create_worker_backend("nope", _settings(Path(temp)))


class ModeTests(unittest.TestCase):
    def test_run_mode_flags_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agy = create_worker_backend("agy", _settings(root))
            cmd_ = agy._cli_command(_job(root, JobMode.RUN), "P")
            self.assertEqual(cmd_[cmd_.index("--mode") + 1], "plan")
            self.assertIn("--sandbox", cmd_)
            cmd = create_worker_backend("cmd", _settings(root))
            c = cmd._cli_command(_job(root, JobMode.RUN), "P")
            self.assertEqual(c[c.index("--permission-mode") + 1], "plan")
            self.assertNotIn("--yolo", c)
            grok = create_worker_backend("grok", _settings(root))
            with self.assertRaisesRegex(Exception, "Super Grok 暂不支持只读提问"):
                grok._cli_command(_job(root, JobMode.RUN), "P")

    def test_fix_mode_agy_keeps_accept_edits_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c = create_worker_backend("agy", _settings(root))._cli_command(_job(root), "P")
            self.assertEqual(c[c.index("--mode") + 1], "accept-edits")
            self.assertIn("--sandbox", c)

    def test_fix_gated_by_unsandboxed_setting(self) -> None:
        from conveyor.agents import workers
        off = _settings(Path("/tmp/x"), unsandboxed=False)
        on = _settings(Path("/tmp/x"), unsandboxed=True)
        for wid in ("cmd", "grok"):
            ok, reason = workers.mode_eligibility(off, wid, JobMode.FIX)
            self.assertFalse(ok)
            self.assertIn("CONVEYOR_WORKERS_UNSANDBOXED", reason)
            self.assertTrue(workers.mode_eligibility(on, wid, JobMode.FIX)[0])
        self.assertTrue(workers.mode_eligibility(off, "agy", JobMode.FIX)[0])
        self.assertTrue(workers.mode_eligibility(off, "cmd", JobMode.RUN)[0])
        self.assertTrue(workers.mode_eligibility(on, "agy", "run")[0])
        ok, reason = workers.mode_eligibility(on, "grok", JobMode.RUN)
        self.assertFalse(ok)
        self.assertEqual(reason, "Super Grok 暂不支持只读提问")

    def test_pick_worker_skips_ineligible_per_mode(self) -> None:
        from conveyor.agents import workers

        def w(wid):
            return {"id": wid, "enabled": True, "installed": True, "runnable": True,
                    "auth": "ok", "quota": {"state": "available"}}
        pool = [w("grok"), w("cmd"), w("agy")]
        off = _settings(Path("/tmp/x"), unsandboxed=False)
        on = _settings(Path("/tmp/x"), unsandboxed=True)
        self.assertEqual(workers.pick_worker(off, pool, mode=JobMode.FIX)["id"], "agy")
        self.assertEqual(workers.pick_worker(off, pool, mode=JobMode.RUN)["id"], "cmd")
        self.assertEqual(workers.pick_worker(on, pool, mode=JobMode.FIX)["id"], "grok")
        self.assertEqual(workers.pick_worker(on, pool, mode=JobMode.RUN)["id"], "cmd")
        self.assertEqual(workers.pick_worker(off, pool)["id"], "grok")  # no mode: unchanged

    def test_backend_refuses_ineligible_mode_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wt"
            root.mkdir()
            backend = create_worker_backend("cmd", _settings(root, unsandboxed=False))
            job = _job(root)

            async def go() -> None:
                async def p(_: str) -> None: ...
                await backend._run_codex_attempt(job, p)
            asyncio.run(go())
            self.assertIn("CONVEYOR_WORKERS_UNSANDBOXED", job.error)
            self.assertEqual(job.return_code, 1)


class RunTests(unittest.TestCase):
    def _run(self, backend: CliBackend, job: Job) -> list[str]:
        progress: list[str] = []

        async def go() -> None:
            async def on_progress(value: str) -> None:
                progress.append(value)
            await backend._run_codex_attempt(job, on_progress)

        asyncio.run(go())
        return progress

    def test_env_filtering_and_success_edits_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wt"
            root.mkdir()
            bindir = Path(temp) / "bin"
            bindir.mkdir()
            _fake_cli(bindir, "agy",
                      "import os, sys\n"
                      "open('edited.txt','w').write('web=%s tg=%s args=%s' % (\n"
                      "  os.environ.get('CONVEYOR_WEB_TOKEN'), os.environ.get('TELEGRAM_BOT_TOKEN'),\n"
                      "  ' '.join(sys.argv[1:3])))\n"
                      "print('working...', flush=True)\n"
                      "print('all done')\n")
            env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                   "CONVEYOR_WEB_TOKEN": "web-secret-token-value",
                   "TELEGRAM_BOT_TOKEN": "tg-secret"}
            with patch.dict(os.environ, env):
                backend = create_worker_backend("agy", _settings(root))
                job = _job(root)
                progress = self._run(backend, job)
            self.assertEqual(job.return_code, 0)
            self.assertFalse(job.error)
            edited = (root / "edited.txt").read_text(encoding="utf-8")
            self.assertIn("web=None", edited)
            self.assertIn("tg=None", edited)
            self.assertIn("-p", edited)
            final = job.final_message_path.read_text(encoding="utf-8")
            self.assertEqual(final, "working...\nall done")
            self.assertTrue(progress)
            self.assertNotIn("web-secret-token-value", job.log_path.read_text(encoding="utf-8"))

    def test_failure_propagates_quota_text(self) -> None:
        from conveyor.agents import workers
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wt"
            root.mkdir()
            bindir = Path(temp) / "bin"
            bindir.mkdir()
            _fake_cli(bindir, "cmd",
                      "import sys\n"
                      "print('Error 429: usage limit reached. Resets in 2h 5m 0s')\n"
                      "sys.exit(3)\n")
            with patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
                backend = create_worker_backend("cmd", _settings(root))
                job = _job(root)
                self._run(backend, job)
            self.assertEqual(job.return_code, 3)
            self.assertIn("Resets in 2h 5m 0s", job.error)
            self.assertLessEqual(len(job.error), 2000)
            classified = workers.classify_failure(job.error)
            self.assertIsNotNone(classified)

    def test_timeout_kills_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "wt"
            root.mkdir()
            bindir = Path(temp) / "bin"
            bindir.mkdir()
            _fake_cli(bindir, "grok", "import time\ntime.sleep(60)\n")
            with patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
                backend = create_worker_backend("grok", _settings(root, timeout=1))
                job = _job(root)
                self._run(backend, job)
            self.assertIn("Timed out", job.error)
            self.assertNotEqual(job.return_code, 0)
            self.assertIsNone(job.process)


if __name__ == "__main__":
    unittest.main()
