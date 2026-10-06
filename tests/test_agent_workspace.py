"""Each agent's jobs run in its own project folder (phase 3)."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents
from agents import AgentStore
from config import load_settings
from runner import CodexRunner
from runner.types import Job, JobMode


def _repo(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)  # noqa: E731
    run("init", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Conveyor Test")
    (path / "README.md").write_text(f"{marker}\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-m", "initial")
    return path.resolve()


class AgentWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.host_repo = _repo(self.root / "host", "host repo")
        self.agent_repo = _repo(self.root / "astra", "astra repo")
        self.stray_repo = _repo(self.root / "stray", "not registered")
        env = {
            "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_ALLOWED_USER_ID": "1",
            "CODEX_WORKSPACE_ROOT": str(self.host_repo),
            "CODEX_TASK_ROOT": str(self.root / "tasks"), "CODEX_MEMORY_ROOT": str(self.root / "memory"),
        }
        with patch.dict(os.environ, env):
            base = load_settings()
        self.settings = dataclasses.replace(
            base, codex_workspace_root=self.host_repo, codex_task_root=self.root / "tasks",
            codex_memory_root=self.root / "memory", codex_retry_429_delays_seconds=[], agents_enabled=True,
        )
        for name in ("worktrees", "logs", "locks"):
            (self.settings.codex_task_root / name).mkdir(parents=True, exist_ok=True)
        self.store = AgentStore(self.settings)
        self.agent = self.store.create({"name": "Astra", "workspace_path": str(self.agent_repo)})
        self.runner = CodexRunner(self.settings)

    def job(self, job_id: str, workspace: Path | None = None) -> Job:
        job = Job(id=job_id, mode=JobMode.RUN, prompt="test", sandbox=JobMode.RUN.sandbox)
        job.workspace_root = workspace
        logs = self.settings.codex_task_root / "logs" / job.id
        logs.mkdir(parents=True, exist_ok=True)
        job.metadata_path = logs / "job.json"
        return job

    def status(self, repo: Path) -> str:
        return subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True).stdout.strip()

    # ---- which repository a conversation works in ----

    def test_conversation_maps_to_the_agents_project_folder(self) -> None:
        chat = agents.chat_id_for(self.agent["id"])
        self.assertEqual(agents.workspace_for_chat(self.settings, "web", chat), self.agent_repo)
        self.assertIsNone(agents.workspace_for_chat(self.settings, "telegram", "42"))      # default agent
        plain = self.store.create({"name": "NoFolder"})
        self.assertIsNone(agents.workspace_for_chat(self.settings, "web", agents.chat_id_for(plain["id"])))
        off = dataclasses.replace(self.settings, agents_enabled=False)
        self.assertIsNone(agents.workspace_for_chat(off, "web", chat))
        self.assertEqual(self.runner._known_workspace_roots(), {self.host_repo, self.agent_repo})

    async def test_only_registered_git_roots_are_accepted(self) -> None:
        self.assertEqual(await self.runner._validated_workspace(self.agent_repo), self.agent_repo)
        for bad in (self.stray_repo, self.root / "missing", self.agent_repo / ".git"):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                await self.runner._validated_workspace(bad)
        (self.root / "plain").mkdir()
        self.store.update(self.agent["id"], {"workspace_path": str(self.root / "plain")})
        with self.assertRaises(Exception):  # registered, but not a git repository
            await self.runner._validated_workspace(self.root / "plain")

    # ---- worktrees ----

    async def test_worktrees_are_cut_from_the_jobs_own_repository(self) -> None:
        mine = await self.runner._create_worktree(self.job("job-agent", self.agent_repo))
        theirs = await self.runner._create_worktree(self.job("job-host"))
        self.assertEqual((mine / "README.md").read_text(), "astra repo\n")
        self.assertEqual((theirs / "README.md").read_text(), "host repo\n")
        self.assertEqual(await self.runner._repo_root_for(mine), self.agent_repo)
        self.assertEqual(await self.runner._repo_root_for(theirs), self.host_repo)

    async def test_an_unknown_repository_never_becomes_an_apply_target(self) -> None:
        stray = self.settings.codex_task_root / "worktrees" / "stray-wt"
        subprocess.run(["git", "worktree", "add", "--detach", str(stray), "HEAD"],
                       cwd=self.stray_repo, check=True, capture_output=True)
        self.assertEqual(await self.runner._repo_root_for(stray), self.host_repo)
        self.assertEqual(await self.runner._repo_root_for(self.root / "nowhere"), self.host_repo)
        from runner import worktree as worktree_module
        with self.assertRaises(RuntimeError):
            await worktree_module._validate_reuse_worktree(self.runner, stray)

    # ---- apply ----

    async def test_apply_lands_in_the_agents_repository_only(self) -> None:
        worktree = await self.runner._create_worktree(self.job("job-agent", self.agent_repo))
        (worktree / "README.md").write_text("astra repo\nedited by the agent\n", encoding="utf-8")
        (worktree / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

        result = await self.runner.apply_job("job-agent", worktree)
        self.assertIn("README.md", self.status(self.agent_repo), result)
        self.assertIn("helper.py", self.status(self.agent_repo), result)
        self.assertEqual((self.agent_repo / "README.md").read_text(), "astra repo\nedited by the agent\n")
        self.assertEqual(self.status(self.host_repo), "")                      # untouched
        self.assertEqual((self.host_repo / "README.md").read_text(), "host repo\n")

    def test_apply_rules_for_an_agents_project_folder(self) -> None:
        from runner.apply_policy import ApplyPolicy

        host = ApplyPolicy(self.settings)
        agent = ApplyPolicy(self.settings, workspace_root=self.agent_repo)
        # Conveyor's own layout allowlist guards the configured workspace only.
        self.assertEqual(host.validate_path("src/app.ts", kind="tracked"), "not in allowlist")
        self.assertIsNone(agent.validate_path("src/app.ts", kind="tracked"))
        # Everything else still applies to an agent's folder.
        for path in (".env", "config/api_token.txt", ".git/config", "node_modules/x/index.js", ".ssh/id_ed25519"):
            self.assertIsNotNone(agent.validate_path(path, kind="tracked"), path)
        self.assertIn("high-risk", agent.validate_path(".github/workflows/ci.yml", kind="tracked"))
        self.assertTrue(ApplyPolicy(self.settings, workspace_root=self.host_repo).enforce_allowlist)

    async def test_dirty_check_looks_at_the_right_repository(self) -> None:
        worktree = await self.runner._create_worktree(self.job("job-agent", self.agent_repo))
        (worktree / "README.md").write_text("changed\n", encoding="utf-8")
        # A dirty host repo is irrelevant to an agent's Apply…
        (self.host_repo / "scratch.txt").write_text("wip\n", encoding="utf-8")
        self.assertNotIn("uncommitted changes", await self.runner.apply_job("job-agent", worktree))

        # …but a dirty agent repo blocks it.
        worktree2 = await self.runner._create_worktree(self.job("job-agent-2", self.agent_repo))
        (worktree2 / "other.py").write_text("X = 1\n", encoding="utf-8")
        self.assertIn("uncommitted changes", await self.runner.apply_job("job-agent-2", worktree2))

    async def test_removal_unregisters_from_the_right_repository(self) -> None:
        worktree = await self.runner._create_worktree(self.job("job-agent", self.agent_repo))
        await self.runner._remove_worktree(worktree)
        self.assertFalse(worktree.exists())
        listed = subprocess.run(["git", "worktree", "list"], cwd=self.agent_repo, capture_output=True, text=True).stdout
        self.assertNotIn("job-agent", listed)

    # ---- the handler passes the workspace only when there is one ----

    async def test_handler_start_call_is_unchanged_without_a_project_folder(self) -> None:
        import inspect

        from handlers import jobs

        source = inspect.getsource(jobs)
        self.assertIn('start_options = {"workspace_root": agent_workspace} if agent_workspace is not None else {}', source)
        self.assertIn("runner.start(mode, effective_body, progress, **start_options)", source)


if __name__ == "__main__":
    unittest.main()
