"""Hermetic tests for the deletion guard rail (runner/delete_guard.py).

The shims are real files executed through ``subprocess``, so these tests cover
the behaviour the pi child actually sees: a refused deletion deletes nothing,
the blocked git forms never reach the real git, and every other git call does.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.core.config import Settings
from runner.delete_guard import (
    BLOCK_EXIT_CODE,
    DELETE_COMMANDS,
    GUARD_MESSAGE,
    make_guard_dir,
    prepend_guard_to_path,
)
from runner.pi_agent import PiBackend
from runner.types import Job, JobMode, JobState


def _base_env(**extra: str) -> dict[str, str]:
    env = {
        "HOME": "/tmp",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
    }
    env.update(extra)
    return env


def _make_settings(tmp: Path) -> Settings:
    root = (tmp / "workspace").resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_root = tmp / "state"
    (state_root / "tasks").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="unused",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=state_root / "tasks",
        codex_model=None,
        codex_timeout_seconds=10,
        telegram_progress_seconds=1,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=state_root / "memory",
        user_timezone="UTC",
        chat_mode="auto",
        chat_api_key="sk-test-deepseek",
        chat_model="deepseek-flash",
        chat_tools_enabled=True,
    )


def _write_fake_pi(tmp: Path, name: str, dump_path: Path) -> Path:
    """Fake pi CLI that records the environment (and guard shims) it was handed."""
    fake_pi = tmp / name
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"marker = {GUARD_MESSAGE.encode('utf-8')!r}\n"
        "first = (os.environ.get('PATH') or '').split(os.pathsep)[0]\n"
        "shims = {}\n"
        "for shim in ('rm', 'rmdir', 'unlink', 'shred', 'git'):\n"
        "    path = os.path.join(first, shim)\n"
        "    try:\n"
        "        with open(path, 'rb') as fh:\n"
        "            shims[shim] = os.path.isfile(path) and marker in fh.read()\n"
        "    except OSError:\n"
        "        shims[shim] = False\n"
        f"with open({str(dump_path)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'env': dict(os.environ), 'first_path': first, 'shims': shims}, fh)\n"
        "print(json.dumps({'type': 'agent_end', 'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}]}]}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    return fake_pi


def _run_pi(backend: PiBackend, job: Job) -> None:
    async def on_progress(text: str) -> None:
        pass

    asyncio.run(backend._run_codex_attempt(job, on_progress))


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


def _git_env(guard_dir: Path, home: Path) -> dict[str, str]:
    """Ambient environment with the guard dir first, isolated from any live repo."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
    }
    env["HOME"] = str(home)
    env["TMPDIR"] = str(home)
    prepend_guard_to_path(env, guard_dir)
    return env


def _git_in(path_entries: list[str]) -> bool:
    return any((Path(entry) / "git").is_file() for entry in path_entries)


class DeleteShimTests(unittest.TestCase):
    def test_make_guard_dir_writes_executable_shims_for_every_delete_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            guard = make_guard_dir(Path(raw) / "guard")
            self.assertTrue(guard.is_dir())
            for name in DELETE_COMMANDS:
                shim = guard / name
                self.assertTrue(shim.is_file(), shim)
                self.assertTrue(os.access(shim, os.X_OK), shim)
            self.assertTrue((guard / "git").is_file())

    def test_delete_shims_refuse_and_delete_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            guard = make_guard_dir(tmp / "guard")
            target = tmp / "keep.txt"
            target.write_text("keep me", encoding="utf-8")
            directory = tmp / "keepdir"
            directory.mkdir()
            (directory / "inner.txt").write_text("inner", encoding="utf-8")

            for name in DELETE_COMMANDS:
                for args in ([str(target)], ["-f", str(target)], ["-rf", str(directory)]):
                    result = _run([str(guard / name), *args])
                    self.assertEqual(result.returncode, BLOCK_EXIT_CODE, (name, args))
                    self.assertEqual(result.stderr.decode("utf-8").strip(), GUARD_MESSAGE, name)
                    self.assertEqual(result.stdout, b"", name)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep me")
            self.assertEqual((directory / "inner.txt").read_text(encoding="utf-8"), "inner")

    def test_delete_shims_are_valid_posix_sh_under_bin_sh_and_dash(self) -> None:
        """Same reason as the git shim: run them through a real /bin/sh (dash)."""
        if not Path("/bin/sh").is_file():
            self.skipTest("/bin/sh is not available")
        shells = ["/bin/sh"]
        dash = shutil.which("dash")
        if dash is not None:
            shells.append(dash)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            guard = make_guard_dir(tmp / "guard")
            target = tmp / "keep.txt"
            target.write_text("keep me", encoding="utf-8")
            for shell in shells:
                with self.subTest(shell=shell):
                    result = _run([shell, str(guard / "rm"), "-rf", str(target)])
                    self.assertEqual(result.returncode, BLOCK_EXIT_CODE, result.stderr)
                    self.assertEqual(result.stderr.decode("utf-8").strip(), GUARD_MESSAGE)
                    self.assertTrue(target.is_file())

    def test_prepend_guard_to_path_puts_the_guard_first_and_drops_empty_entries(self) -> None:
        env = {"PATH": os.pathsep.join(["/usr/bin", "", "/bin"])}
        updated = prepend_guard_to_path(env, Path("/tmp/guard"))
        self.assertEqual(updated.split(os.pathsep)[0], "/tmp/guard")
        self.assertNotIn("", updated.split(os.pathsep))
        self.assertIn("/usr/bin", updated.split(os.pathsep))
        self.assertIn("/bin", updated.split(os.pathsep))


class GitShimTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.guard = make_guard_dir(self.tmp / "guard")
        self.env = _git_env(self.guard, self.tmp)
        self.git = str(self.guard / "git")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        init = _run([shutil.which("git") or "git", "init", "-q"], cwd=repo, env=self.env)
        self.assertEqual(init.returncode, 0, init.stderr)
        return repo

    def test_blocks_destructive_git_forms(self) -> None:
        blocked = [
            ["clean"],
            ["clean", "-fdx"],
            ["-C", ".", "clean"],
            ["reset", "--hard"],
            ["reset", "--hard", "HEAD"],
            ["push", "--force"],
            ["push", "-f"],
            ["push", "origin", "main", "--force-with-lease"],
            ["branch", "-D", "feature"],
            ["worktree", "remove", "wt"],
        ]
        for args in blocked:
            result = _run([self.git, *args], cwd=self._repo(), env=self.env)
            self.assertEqual(result.returncode, BLOCK_EXIT_CODE, args)
            self.assertEqual(result.stderr.decode("utf-8").strip(), GUARD_MESSAGE, args)
            self.assertEqual(result.stdout, b"", args)

    def test_git_clean_leaves_the_file_in_place(self) -> None:
        repo = self._repo()
        untracked = repo / "leftover.txt"
        untracked.write_text("still here", encoding="utf-8")
        result = _run([self.git, "clean", "-fd"], cwd=repo, env=self.env)
        self.assertEqual(result.returncode, BLOCK_EXIT_CODE)
        self.assertEqual(untracked.read_text(encoding="utf-8"), "still here")

    def test_passes_read_only_git_through_to_the_real_git(self) -> None:
        version = _run([self.git, "--version"], env=self.env)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn("git version", version.stdout.decode("utf-8", errors="replace"))

        repo = self._repo()
        (repo / "file.txt").write_text("hello", encoding="utf-8")
        status = _run([self.git, "status", "--short"], cwd=repo, env=self.env)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("file.txt", status.stdout.decode("utf-8", errors="replace"))

    def test_git_shim_is_valid_posix_sh_under_bin_sh_and_dash(self) -> None:
        """Run the shim through a shell explicitly: a syntax error would show up here.

        The VPS /bin/sh is dash, and dash rejects a case pattern's ')' that
        lands inside a command substitution, which is exactly how an earlier
        version of this shim broke.
        """
        if not Path("/bin/sh").is_file():
            self.skipTest("/bin/sh is not available")
        shells = ["/bin/sh"]
        dash = shutil.which("dash")
        if dash is not None:
            shells.append(dash)
        for shell in shells:
            with self.subTest(shell=shell):
                version = _run([shell, str(self.guard / "git"), "--version"], env=self.env)
                self.assertEqual(version.returncode, 0, version.stderr)
                self.assertIn("git version", version.stdout.decode("utf-8", errors="replace"))
                status = _run(
                    [shell, str(self.guard / "git"), "status", "--short"],
                    cwd=self._repo(),
                    env=self.env,
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                blocked = _run(
                    [shell, str(self.guard / "git"), "reset", "--hard"],
                    cwd=self._repo(),
                    env=self.env,
                )
                self.assertEqual(blocked.returncode, BLOCK_EXIT_CODE, blocked.stderr)
                self.assertEqual(blocked.stderr.decode("utf-8").strip(), GUARD_MESSAGE)
                self.assertEqual(blocked.stdout, b"")


class PiBackendGuardTests(unittest.TestCase):
    def _job(self, settings: Settings, tmp: Path, **extra) -> Job:
        return Job(
            "job-guard",
            JobMode.RUN,
            "qa-todo-cli 进度怎么样了",
            "unused",
            state=JobState.RUNNING,
            worktree_path=settings.codex_workspace_root,
            log_path=settings.codex_task_root / "job.log",
            final_message_path=settings.codex_task_root / "final.txt",
            **extra,
        )

    def test_non_confirmed_job_runs_with_the_guard_dir_first_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            dump = tmp / "env.json"
            fake_pi = _write_fake_pi(tmp, "fake-pi-guard", dump)
            with patch.dict(os.environ, _base_env(), clear=True):
                backend = PiBackend(settings)
                backend.pi_bin = str(fake_pi)
                job = self._job(settings, tmp)
                _run_pi(backend, job)
            self.assertEqual(job.return_code, 0, job.error)

            dump_data = json.loads(dump.read_text(encoding="utf-8"))
            child_env = dump_data["env"]
            guard_dir = Path(dump_data["first_path"])
            # First on PATH, so the shims win over the real binaries...
            self.assertEqual(child_env["PATH"].split(os.pathsep)[0], str(guard_dir))
            # ...and the real guard shims were there while pi ran.
            for shim in ("rm", "rmdir", "unlink", "shred"):
                self.assertTrue(dump_data["shims"][shim], shim)
            self.assertEqual(
                dump_data["shims"]["git"], _git_in(_base_env()["PATH"].split(os.pathsep))
            )
            # The real PATH entries survive behind the guard dir.
            self.assertIn("/usr/bin", child_env["PATH"].split(os.pathsep))
            # ...and the guard directory is cleaned up once the run is over.
            self.assertFalse(guard_dir.exists())

    def test_confirmed_job_gets_no_guard_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            dump = tmp / "env-confirmed.json"
            fake_pi = _write_fake_pi(tmp, "fake-pi-confirmed", dump)
            with patch.dict(os.environ, _base_env(), clear=True):
                backend = PiBackend(settings)
                backend.pi_bin = str(fake_pi)
                job = self._job(settings, tmp, destructive_confirmed=True)
                _run_pi(backend, job)
            self.assertEqual(job.return_code, 0, job.error)

            dump_data = json.loads(dump.read_text(encoding="utf-8"))
            child_env = dump_data["env"]
            # No guard directory anywhere on PATH: the approved job may delete.
            self.assertEqual(dump_data["first_path"], "/usr/bin")
            self.assertFalse(any(dump_data["shims"].values()), dump_data["shims"])
            self.assertEqual(child_env["PATH"].split(os.pathsep)[0], "/usr/bin")

    def test_question_only_prompt_tells_the_agent_to_only_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = PiBackend(_make_settings(Path(raw)))
            job = Job(id="j1", mode=JobMode.RUN, prompt="进度怎么样", sandbox="workspace-write")
            prompt = backend._pi_system_prompt(job)
            self.assertIn(
                "If the user is only asking a question (status, progress, explanation), "
                "only read: do not create, modify, move or delete anything.",
                prompt,
            )
            confirmed = Job(
                id="j2", mode=JobMode.RUN, prompt="删掉它", sandbox="workspace-write",
                destructive_confirmed=True,
            )
            self.assertNotIn("only read", backend._pi_system_prompt(confirmed))


if __name__ == "__main__":
    unittest.main()
