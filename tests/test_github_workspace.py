"""Tests for workspace folder and GitHub repository (gh connect) binding."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import request as urllib_request

import agents
import github_workspace
from config import load_settings
from handlers.job_queue import JobQueue
from transcript_store import get_transcript_store
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


def _init_git_repo(path: Path, remote_url: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)  # noqa: E731
    run("init", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Conveyor Test")
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-m", "initial")
    if remote_url:
        run("remote", "add", "origin", remote_url)
    return path.resolve()


class _FakeRunner:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.started_workspaces: list[Path | None] = []

    async def queue_snapshot(self) -> dict[str, object]:
        return {"depth": 0, "paused": False, "states": {}}

    async def list_jobs(self, limit: int = 50) -> list[dict[str, object]]:
        return []

    async def get_job(self, job_id: str) -> dict[str, object] | None:
        return None

    async def start(self, mode, prompt, progress, **kwargs):
        self.started_workspaces.append(kwargs.get("workspace_root"))
        return type("Result", (), {"status": "completed", "summary": "done", "job_id": "job-1"})()


class GithubWorkspaceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.host_repo = _init_git_repo(self.root / "Conveyor", "https://github.com/mammut001/Conveyor.git")
        self.other_repo = _init_git_repo(self.root / "FocusApp", "git@github.com:mammut001/FocusApp.git")
        env = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": "1",
            "CODEX_WORKSPACE_ROOT": str(self.host_repo),
            "CODEX_TASK_ROOT": str(self.root / "tasks"),
            "CODEX_MEMORY_ROOT": str(self.root / "memory"),
        }
        with patch.dict(os.environ, env):
            base = load_settings()
        self.token = "x" * 36
        self.settings = dataclasses.replace(
            base,
            codex_workspace_root=self.host_repo,
            codex_task_root=self.root / "tasks",
            codex_memory_root=self.root / "memory",
            agents_enabled=False,
        )
        for sub in ("worktrees", "logs", "locks"):
            (self.settings.codex_task_root / sub).mkdir(parents=True, exist_ok=True)
        github_workspace.invalidate_gh_cache()

    def test_detect_git_remote_and_validate_local_folder(self) -> None:
        self.assertEqual(github_workspace.detect_git_remote_repo(self.host_repo), "mammut001/Conveyor")
        self.assertEqual(github_workspace.detect_git_remote_repo(self.other_repo), "mammut001/FocusApp")

        info = github_workspace.validate_local_folder(str(self.other_repo))
        self.assertEqual(info["workspace_path"], str(self.other_repo))
        self.assertEqual(info["workspace_status"], "ok")
        self.assertEqual(info["github_repo"], "mammut001/FocusApp")

        plain_dir = self.root / "plain-folder"
        plain_dir.mkdir()
        plain_info = github_workspace.validate_local_folder(str(plain_dir))
        self.assertEqual(plain_info["workspace_status"], "not_git")
        self.assertEqual(plain_info["github_repo"], "")

    def test_list_gh_repos_cross_references_local_checkouts(self) -> None:
        fake_gh_json = json.dumps([
            {
                "nameWithOwner": "mammut001/Conveyor",
                "name": "Conveyor",
                "description": "Personal agent console",
                "isPrivate": False,
                "updatedAt": "2026-11-09T00:00:00Z",
                "defaultBranchRef": {"name": "main"},
                "url": "https://github.com/mammut001/Conveyor",
            },
            {
                "nameWithOwner": "mammut001/RemoteOnlyRepo",
                "name": "RemoteOnlyRepo",
                "description": "Not cloned yet",
                "isPrivate": True,
                "updatedAt": "2026-11-08T00:00:00Z",
                "defaultBranchRef": {"name": "main"},
                "url": "https://github.com/mammut001/RemoteOnlyRepo",
            },
        ])
        fake_proc = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=fake_gh_json, stderr="")
        with patch.object(
            github_workspace,
            "gh_auth_status",
            return_value={"installed": True, "connected": True, "account": "mammut001", "error": ""},
        ), patch("shutil.which", return_value="/opt/homebrew/bin/gh"), patch.object(
            github_workspace.subprocess,
            "run",
            return_value=fake_proc,
        ):
            payload = github_workspace.list_gh_repos(self.settings, refresh=True)
            self.assertEqual(payload["count"], 2)
            self.assertTrue(payload["auth"]["authenticated"])
            self.assertEqual(payload["auth"]["account"], "mammut001")
            by_name = {r["full_name"]: r for r in payload["repos"]}
            self.assertTrue(by_name["mammut001/Conveyor"]["cloned"])
            self.assertEqual(by_name["mammut001/Conveyor"]["local_path"], str(self.host_repo))
            self.assertFalse(by_name["mammut001/RemoteOnlyRepo"]["cloned"])
            self.assertEqual(by_name["mammut001/RemoteOnlyRepo"]["local_path"], "")

    def test_ensure_github_repo_workspace_reuses_or_clones(self) -> None:
        # 1. Existing local checkout is reused without invoking gh repo clone
        resolved = github_workspace.ensure_github_repo_workspace(self.settings, "mammut001/FocusApp")
        self.assertEqual(resolved["workspace_path"], str(self.other_repo))
        self.assertEqual(resolved["github_repo"], "mammut001/FocusApp")
        self.assertFalse(resolved["cloned_now"])

        # 2. Remote-only repo triggers gh repo clone into <codex_task_root>/repos/<owner>/<repo>
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 4 and cmd[1:3] == ["repo", "clone"]:
                target = Path(cmd[-1])
                _init_git_repo(target, "https://github.com/mammut001/NewCloudRepo.git")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cloned", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch("shutil.which", return_value="/opt/homebrew/bin/gh"), patch.object(
            github_workspace.subprocess, "run", side_effect=fake_run
        ):
            cloned = github_workspace.ensure_github_repo_workspace(self.settings, "mammut001/NewCloudRepo")
            expected_path = (self.settings.codex_task_root / "repos" / "mammut001" / "NewCloudRepo").resolve()
            self.assertEqual(cloned["workspace_path"], str(expected_path))
            self.assertEqual(cloned["github_repo"], "mammut001/NewCloudRepo")
            self.assertTrue(cloned["cloned_now"])

    def test_session_binding_integrates_with_agents_workspace_for_chat(self) -> None:
        store = get_transcript_store(self.settings)
        bound = store.bind_workspace(
            "web:1:webchat-test1",
            channel="web",
            operator_id="1",
            source_chat_id="webchat-test1",
            workspace_path=str(self.other_repo),
            github_repo="mammut001/FocusApp",
            workspace_source="github",
        )
        self.assertEqual(bound["workspace_path"], str(self.other_repo))
        self.assertEqual(bound["github_repo"], "mammut001/FocusApp")

        # Even with agents_enabled=False, session binding resolves workspace_for_chat & github_repo_for_chat
        self.assertEqual(
            agents.workspace_for_chat(self.settings, "web", "webchat-test1"),
            self.other_repo,
        )
        self.assertEqual(
            agents.github_repo_for_chat(self.settings, "web", "webchat-test1"),
            "mammut001/FocusApp",
        )
        self.assertIn(self.other_repo, agents.workspace_roots(self.settings))

        prompt_block = github_workspace.workspace_prompt_block(self.settings, "web", "webchat-test1")
        self.assertIn("mammut001/FocusApp", prompt_block)
        self.assertIn(str(self.other_repo), prompt_block)

    def test_web_api_bind_and_list_endpoints(self) -> None:
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        runner = _FakeRunner(self.settings)
        queue = JobQueue(self.settings)
        control = WebControl(self.settings, runner, queue)
        server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=control,
            loop=loop,
            token=self.token,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def cleanup_server() -> None:
            server.shutdown()
            server.server_close()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            server_thread.join(timeout=2)

        self.addCleanup(cleanup_server)
        port = server.server_address[1]

        def req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            r = urllib_request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib_request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))

        status, local_data = req("GET", "/api/workspaces/local")
        self.assertEqual(status, 200)
        paths = {w["path"] for w in local_data["workspaces"]}
        self.assertIn(str(self.host_repo), paths)
        self.assertIn(str(self.other_repo), paths)

        # Bind a new chat session to FocusApp via GitHub source
        status, bind_res = req(
            "POST",
            "/api/sessions/bind",
            {
                "chat_mode": True,
                "source": "github",
                "github_repo": "mammut001/FocusApp",
            },
        )
        self.assertEqual(status, 200)
        session_id = bind_res["session_id"]
        self.assertTrue(session_id.startswith("web:web-console:webchat-"), session_id)
        self.assertEqual(bind_res["workspace_path"], str(self.other_repo))
        self.assertEqual(bind_res["github_repo"], "mammut001/FocusApp")
        self.assertEqual(bind_res["workspace_status"], "ok")

        # Verify /api/chat/history returns the bound workspace metadata
        status, hist = req("GET", f"/api/chat/history?session_id={session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(hist["workspace_path"], str(self.other_repo))
        self.assertEqual(hist["github_repo"], "mammut001/FocusApp")
        self.assertEqual(hist["workspace_source"], "github")

        # Verify unbinding clears the workspace
        status, clear_res = req(
            "POST",
            "/api/sessions/bind",
            {
                "session_id": session_id,
                "source": "clear",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(clear_res["workspace_path"], "")
        self.assertEqual(clear_res["github_repo"], "")


if __name__ == "__main__":
    unittest.main()
