"""tests/test_supervisor_api.py — End-to-end authenticated HTTP + process tests
for the Agent Supervisor Web/API integration.

Runs real `WebConsoleServer` + `WebConsoleHandler` over loopback HTTP against
disposable git repositories, isolated SQLite databases, and local fake CLI
executables (process integration only; zero external LLM calls).
"""
from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from conveyor.agents import agent_supervisor as ags
from config import Settings
from handlers.job_queue import JobQueue
from web_console import WebConsoleHandler, WebConsoleServer
from conveyor.web.web_control import WebControl

TOKEN = "sup-http-test-token-0123456789-abcdefghijklmnop"


def _make_settings(
    root: Path,
    *,
    supervisor_enabled: bool = True,
    chat_enabled_flag: bool = True,
    validation_commands: tuple[Any, ...] = (),
) -> Settings:
    workspace = root / "workspace"
    task_root = root / "tasks"
    mem_root = root / "memory"
    for d in (workspace, task_root, mem_root):
        d.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=1,
        codex_workspace_root=workspace,
        codex_bin="/bin/echo",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=5,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem_root,
        user_timezone="UTC",
        conveyor_web_enabled=True,
        conveyor_web_host="127.0.0.1",
        conveyor_web_port=0,
        conveyor_web_token=TOKEN,
        chat_mode="auto" if chat_enabled_flag else "off",
        chat_base_url="http://127.0.0.1:9/v1" if chat_enabled_flag else None,
        chat_api_key="test-local-chat-key" if chat_enabled_flag else None,
        chat_model="test-coordinator-model" if chat_enabled_flag else None,
        chat_tools_enabled=True,
        agent_supervisor_enabled=supervisor_enabled,
        agent_supervisor_analyst_executor="claude",
        agent_supervisor_implementer_executor="claude",
        agent_supervisor_reviewer_executor="claude",
        agent_supervisor_max_repair_rounds=1,
        agent_supervisor_task_timeout_seconds=25,
        agent_supervisor_validation_commands=validation_commands,
    )


def _init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Supervisor Test"], check=True)
    (repo_dir / "README.md").write_text("# Disposable Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
    head = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head


def _write_fake_claude_cli(script_path: Path, *, mode: str = "normal", pid_marker: Path | None = None) -> Path:
    """Create a real executable Python CLI mimicking `claude -p --output-format stream-json`."""
    marker_str = str(pid_marker) if pid_marker else ""
    code = f"""#!{sys.executable}
import json
import os
import pathlib
import re
import subprocess
import sys
import time

MODE = {mode!r}
PID_MARKER = {marker_str!r}

argv = sys.argv[1:]
prompt = sys.stdin.read()

if PID_MARKER:
    pathlib.Path(PID_MARKER).write_text(str(os.getpid()), encoding="utf-8")

if MODE == "slow":
    time.sleep(30.0)
    sys.exit(0)

is_write = "acceptEdits" in argv
if "You are the analyst." in prompt:
    out = "1. Add feature.txt in managed worktree.\\n2. Commit cleanly and verify."
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": out}}))
    sys.exit(0)

if is_write and "You are the implementer." in prompt:
    cwd = pathlib.Path.cwd()
    target = cwd / "feature.txt"
    target.write_text("implemented-via-supervisor-worker\\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.email", "impl@example.com"], cwd=str(cwd), check=True)
    subprocess.run(["git", "config", "user.name", "Implementer Bot"], cwd=str(cwd), check=True)
    subprocess.run(["git", "add", "feature.txt"], cwd=str(cwd), check=True)
    subprocess.run(["git", "commit", "-m", "feat: add feature.txt"], cwd=str(cwd), check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()
    summary = f"Added feature.txt and committed {{sha}}"
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": summary}}))
    sys.exit(0)

if "INDEPENDENT code reviewer" in prompt:
    m_sha = re.search(r"Current SHA:\\s*([0-9a-f]{{40}})", prompt)
    m_fp = re.search(r"Target fingerprint:\\s*([0-9a-f]{{64}})", prompt)
    sha = m_sha.group(1) if m_sha else ""
    fp = m_fp.group(1) if m_fp else ""
    verdict = {{
        "passed": True,
        "target_sha": sha,
        "target_fingerprint": fp,
        "issues": [],
        "checks": [
            {{
                "name": "inspect-feature-file",
                "command": "read feature.txt",
                "passed": True,
                "output": "implemented-via-supervisor-worker",
            }}
        ],
        "summary": "Verified feature.txt content and commit cleanly in read-only review.",
    }}
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(verdict)}}))
    sys.exit(0)

print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}}))
"""
    script_path.write_text(code, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


class FakeRunner:
    name = "codex"


class _HttpHarness:
    """Starts a real loopback `WebConsoleServer` + dedicated asyncio loop thread."""

    def __init__(self, settings: Settings, *, runner: Any = None) -> None:
        self.settings = settings
        self.runner = runner if runner is not None else FakeRunner()
        self.queue = JobQueue()
        self.queue.configure(settings, self.runner, recover=False)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.loop_thread.start()
        self.control = WebControl(settings, self.runner, self.queue)
        self.server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=self.control,
            loop=self.loop,
            token=TOKEN,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = int(self.server.server_address[1])

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=3.0)
        self.server_thread.join(timeout=3.0)
        if not self.loop.is_closed():
            self.loop.close()

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        authorized: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers: dict[str, str] = {}
            payload: bytes | None = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(payload))
            if authorized:
                headers["Authorization"] = f"Bearer {TOKEN}"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return resp.status, data
        finally:
            conn.close()

    def request_sse(
        self,
        path: str,
        body: dict[str, Any],
        *,
        authorized: bool = True,
    ) -> tuple[int, list[tuple[str, dict[str, Any]]] | dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(body).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            }
            if authorized:
                headers["Authorization"] = f"Bearer {TOKEN}"
            conn.request("POST", path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.getheader("Content-Type") or ""
            if "text/event-stream" not in ctype:
                return resp.status, (json.loads(raw) if raw else {})
            events: list[tuple[str, dict[str, Any]]] = []
            for block in raw.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                ev_name = "message"
                ev_data = "{}"
                for ln in block.splitlines():
                    if ln.startswith("event:"):
                        ev_name = ln[len("event:"):].strip()
                    elif ln.startswith("data:"):
                        ev_data = ln[len("data:"):].strip()
                events.append((ev_name, json.loads(ev_data)))
            return resp.status, events
        finally:
            conn.close()


class SupervisorApiHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-sup-api-")
        self.root = Path(self._tmp.name)
        self._harnesses: list[_HttpHarness] = []
        self._env_backup = os.environ.copy()
        os.environ.pop("CONVEYOR_AGENT_SUPERVISOR_ENABLED", None)
        os.environ.pop("CONVEYOR_SUPERVISOR_ENABLED", None)
        os.environ.pop("CONVEYOR_CLAUDE_BIN", None)
        os.environ.pop("CONVEYOR_COMMAND_CODE_BIN", None)
        os.environ.pop("CONVEYOR_ANTIGRAVITY_BIN", None)

    def tearDown(self) -> None:
        for h in reversed(self._harnesses):
            h.close()
        os.environ.clear()
        os.environ.update(self._env_backup)
        self._tmp.cleanup()

    def _start_server(self, settings: Settings, *, runner: Any = None) -> _HttpHarness:
        h = _HttpHarness(settings, runner=runner)
        self._harnesses.append(h)
        return h

    # ------------------------------------------------------------------
    # 1. Auth 401 + Feature-off 409 + system_status boolean flag
    # ------------------------------------------------------------------

    def test_auth_401_and_feature_disabled_409_and_system_status(self) -> None:
        off_settings = _make_settings(self.root / "off", supervisor_enabled=False)
        off = self._start_server(off_settings)

        st_code, sys_status = off.request_json("GET", "/api/system/status")
        self.assertEqual(st_code, 200)
        self.assertIs(sys_status["features"]["agent_supervisor"], False)

        get_routes = [
            "/api/supervisor/capabilities",
            "/api/supervisor/repositories",
            "/api/supervisor/tasks",
            "/api/supervisor/tasks/task-123",
            "/api/supervisor/deployments",
        ]
        post_routes = [
            ("/api/supervisor/repositories", {"path": "/tmp"}),
            ("/api/supervisor/repositories/repo-1/worktrees", {"branch": "feat/a"}),
            ("/api/supervisor/tasks", {"goal": "x", "repo_id": "repo-1", "worktree_id": "wt-1"}),
            ("/api/supervisor/tasks/task-1/dispatch", {}),
            ("/api/supervisor/tasks/task-1/cancel", {}),
            ("/api/supervisor/tasks/task-1/transition", {"state": "completed"}),
            ("/api/supervisor/deployments", {"task_id": "task-1"}),
            ("/api/supervisor/deployments/dep-1/stage", {"stage": "verify", "status": "succeeded"}),
        ]

        for route in get_routes:
            code_unauth, body_unauth = off.request_json("GET", route, authorized=False)
            self.assertEqual(code_unauth, 401, route)
            self.assertEqual(body_unauth.get("error"), "unauthorized")

            code_off, body_off = off.request_json("GET", route)
            self.assertEqual(code_off, 409, route)
            self.assertEqual(body_off.get("code"), "agent_supervisor_disabled")

        for route, payload in post_routes:
            code_unauth, _ = off.request_json("POST", route, payload, authorized=False)
            self.assertEqual(code_unauth, 401, route)

            code_off, body_off = off.request_json("POST", route, payload)
            self.assertEqual(code_off, 409, route)
            self.assertEqual(body_off.get("code"), "agent_supervisor_disabled")

        # Supervisor session IDs also answer 409 when feature is disabled, while normal chat works.
        c_hist, b_hist = off.request_json("GET", "/api/chat/history?session_id=web:web-console:supwt-wt-123")
        self.assertEqual(c_hist, 409)
        self.assertEqual(b_hist.get("code"), "agent_supervisor_disabled")

        c_chat, b_chat = off.request_json(
            "POST",
            "/api/chat",
            {"message": "hello", "session_id": "web:web-console:supwt-wt-123"},
        )
        self.assertEqual(c_chat, 409)
        self.assertEqual(b_chat.get("code"), "agent_supervisor_disabled")

        c_norm_hist, b_norm_hist = off.request_json("GET", "/api/chat/history?session_id=webchat-normal")
        self.assertEqual(c_norm_hist, 200)
        self.assertEqual(b_norm_hist["messages"], [])

        # Enabled server reports features.agent_supervisor == True.
        on_settings = _make_settings(self.root / "on", supervisor_enabled=True)
        on = self._start_server(on_settings)
        st_on, sys_on = on.request_json("GET", "/api/system/status")
        self.assertEqual(st_on, 200)
        self.assertIs(sys_on["features"]["agent_supervisor"], True)

    # ------------------------------------------------------------------
    # 2. Repository registration validation, path traversal, /tmp == /private/tmp
    # ------------------------------------------------------------------

    def test_repository_registration_validation_traversal_and_canonical_dedup(self) -> None:
        settings = _make_settings(self.root / "env")
        srv = self._start_server(settings)

        # Initially empty: no host filesystem scanning.
        code, initial = srv.request_json("GET", "/api/supervisor/repositories")
        self.assertEqual(code, 200)
        self.assertEqual(initial, {"repositories": []})

        repo_dir = self.root / "sample-repo"
        _init_git_repo(repo_dir)
        sub_dir = repo_dir / "subdir"
        sub_dir.mkdir()
        plain_dir = self.root / "not-a-git-repo"
        plain_dir.mkdir()

        # Reject relative paths, traversal segments, non-git dirs, and git subdirectories.
        for bad_path in (
            "relative/repo",
            f"{repo_dir}/../sample-repo",
            str(plain_dir),
            str(sub_dir),
            str(self.root / "does-not-exist"),
        ):
            c_bad, b_bad = srv.request_json("POST", "/api/supervisor/repositories", {"path": bad_path})
            self.assertEqual(c_bad, 400, f"expected 400 for {bad_path!r}, got {c_bad}: {b_bad}")

        # Verify /tmp vs /private/tmp canonical equivalence on macOS (or symlink alias).
        canon_str = os.path.realpath(str(repo_dir))
        if canon_str.startswith("/private/tmp/"):
            alias_path = "/tmp/" + canon_str[len("/private/tmp/"):]
        else:
            symlink_alias = self.root / "repo-symlink"
            symlink_alias.symlink_to(repo_dir)
            alias_path = str(symlink_alias)

        c1, r1 = srv.request_json("POST", "/api/supervisor/repositories", {"path": alias_path})
        self.assertEqual(c1, 201)
        self.assertEqual(r1["path"], canon_str)
        self.assertEqual(len(r1["worktrees"]), 1)
        self.assertTrue(r1["worktrees"][0]["is_main"])
        self.assertFalse(r1["worktrees"][0]["managed"])

        # Re-registering via canonical path is idempotent (same ID, zero duplicate rows).
        c2, r2 = srv.request_json("POST", "/api/supervisor/repositories", {"path": canon_str})
        self.assertEqual(c2, 200)
        self.assertEqual(r2["id"], r1["id"])

        c_list, listed = srv.request_json("GET", "/api/supervisor/repositories")
        self.assertEqual(c_list, 200)
        self.assertEqual(len(listed["repositories"]), 1)
        self.assertEqual(listed["repositories"][0]["id"], r1["id"])

    # ------------------------------------------------------------------
    # 3. Worktree enumeration + actual managed worktree creation + branch safety
    # ------------------------------------------------------------------

    def test_worktree_enumeration_and_managed_creation_safety(self) -> None:
        settings = _make_settings(self.root / "wt-env")
        srv = self._start_server(settings)

        repo_dir = self.root / "wt-repo"
        init_sha = _init_git_repo(repo_dir)

        # Create an unmanaged external git worktree manually.
        ext_wt = self.root / "external-unmanaged-wt"
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "-b", "ext-branch", str(ext_wt)],
            check=True,
            capture_output=True,
        )

        # Linked worktree cannot be registered as a standalone main repository.
        c_linked, _ = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(ext_wt)})
        self.assertEqual(c_linked, 400)

        c_reg, repo = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        self.assertEqual(c_reg, 201)
        by_branch = {w["branch"]: w for w in repo["worktrees"]}
        self.assertIn("main", by_branch)
        self.assertIn("ext-branch", by_branch)
        self.assertTrue(by_branch["main"]["is_main"])
        self.assertFalse(by_branch["main"]["managed"])
        self.assertFalse(by_branch["ext-branch"]["is_main"])
        self.assertFalse(by_branch["ext-branch"]["managed"])

        # Reject option injection / invalid git ref names.
        for bad_payload in (
            {"branch": "--detach"},
            {"branch": "-b"},
            {"branch": "bad..branch"},
            {"branch": "bad branch name"},
            {"branch": "feat/ok", "base_ref": "--all"},
            {"branch": "feat/ok", "base_ref": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
        ):
            c_bad, _ = srv.request_json(
                "POST",
                f"/api/supervisor/repositories/{repo['id']}/worktrees",
                bad_payload,
            )
            self.assertEqual(c_bad, 400, f"expected 400 for {bad_payload}")

        # Create an actual managed worktree via API.
        c_wt, created_wt = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/managed-one", "base_ref": "main"},
        )
        self.assertEqual(c_wt, 201, created_wt)
        self.assertEqual(created_wt["repo_id"], repo["id"])
        self.assertEqual(created_wt["branch"], "feat/managed-one")
        self.assertEqual(created_wt["head"], init_sha)
        self.assertFalse(created_wt["is_main"])
        self.assertTrue(created_wt["managed"])
        self.assertFalse(created_wt["locked"])
        self.assertTrue(created_wt["chat_session_id"].startswith("web:web-console:supwt-wt-"))
        self.assertTrue(Path(created_wt["path"]).is_dir())
        ok_policy, reason_policy = ags.validate_worktree(settings, repo["path"], created_wt["path"])
        self.assertTrue(ok_policy, reason_policy)

    # ------------------------------------------------------------------
    # 4. Task scoping, read-only main/unmanaged guard, idempotent request_id,
    #    server-enforced role overrides, disabled transitions & deployments
    # ------------------------------------------------------------------

    def test_task_scoping_idempotency_role_overrides_and_disabled_mutations(self) -> None:
        fake_claude = _write_fake_claude_cli(self.root / "fake-claude")
        os.environ["CONVEYOR_CLAUDE_BIN"] = str(fake_claude)
        os.environ["CONVEYOR_COMMAND_CODE_BIN"] = "/nonexistent/command-code-bin"
        os.environ["CONVEYOR_ANTIGRAVITY_BIN"] = "/nonexistent/agy-bin"

        settings = _make_settings(self.root / "scope-env")
        srv = self._start_server(settings)

        # Capabilities reflect actual adapter availability & unconfigured deployment.
        c_cap, caps = srv.request_json("GET", "/api/supervisor/capabilities")
        self.assertEqual(c_cap, 200)
        self.assertTrue(caps["enabled"])
        exec_by_name = {e["name"]: e for e in caps["executors"]}
        self.assertTrue(exec_by_name["claude"]["available"])
        self.assertFalse(exec_by_name["command-code"]["available"])
        self.assertFalse(exec_by_name["codex"]["available"])
        self.assertIn("Codex", exec_by_name["codex"]["reason"])
        self.assertNotIn("models", exec_by_name["claude"])
        self.assertFalse(caps["deployment"]["configured"])
        self.assertFalse(caps["deployment"]["can_start"])
        self.assertTrue(caps["deployment"]["reason"])
        self.assertEqual(caps["deployment"]["source_machine"], socket.gethostname().strip())
        self.assertNotIn("target_machine", caps["deployment"])

        repo_dir = self.root / "scope-repo"
        _init_git_repo(repo_dir)
        ext_wt = self.root / "scope-ext-wt"
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "-b", "ext", str(ext_wt)],
            check=True,
            capture_output=True,
        )
        _, repo = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        main_wt = next(w for w in repo["worktrees"] if w["is_main"])
        unmanaged_wt = next(w for w in repo["worktrees"] if w["branch"] == "ext")

        _, wt_a = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/wt-a"},
        )
        _, wt_b = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/wt-b"},
        )

        # Main and unmanaged worktrees cannot accept tasks.
        for ro_wt in (main_wt, unmanaged_wt):
            c_ro, b_ro = srv.request_json(
                "POST",
                "/api/supervisor/tasks",
                {"goal": "write on ro", "repo_id": repo["id"], "worktree_id": ro_wt["id"]},
            )
            self.assertEqual(c_ro, 409, b_ro)
            self.assertEqual(b_ro.get("code"), "unmanaged_worktree")

        # Cross-worktree session_id is rejected.
        c_sess, b_sess = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "goal": "wrong session",
                "repo_id": repo["id"],
                "worktree_id": wt_a["id"],
                "session_id": wt_b["chat_session_id"],
            },
        )
        self.assertEqual(c_sess, 400)
        self.assertEqual(b_sess.get("code"), "session_mismatch")

        # Create task with request_id: stays pending_dispatch (does NOT start worker).
        c_t1, t1 = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "goal": "Implement scoped task A",
                "repo_id": repo["id"],
                "worktree_id": wt_a["id"],
                "session_id": wt_a["chat_session_id"],
                "acceptance_criteria": "feature.txt exists",
                "request_id": "req-idem-001",
            },
        )
        self.assertEqual(c_t1, 201)
        self.assertEqual(t1["state"], "pending_dispatch")
        self.assertEqual(t1["metadata"]["repo_id"], repo["id"])
        self.assertEqual(t1["metadata"]["worktree_id"], wt_a["id"])

        # Retry with identical request_id + payload returns same task without duplicate.
        c_t1_retry, t1_retry = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "goal": "Implement scoped task A",
                "repo_id": repo["id"],
                "worktree_id": wt_a["id"],
                "acceptance_criteria": "feature.txt exists",
                "request_id": "req-idem-001",
            },
        )
        self.assertEqual(c_t1_retry, 200)
        self.assertEqual(t1_retry["id"], t1["id"])

        # Reusing same request_id with different goal fails with 409 conflict.
        c_t1_conf, b_t1_conf = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "goal": "Different goal reusing request_id",
                "repo_id": repo["id"],
                "worktree_id": wt_a["id"],
                "request_id": "req-idem-001",
            },
        )
        self.assertEqual(c_t1_conf, 409)
        self.assertEqual(b_t1_conf.get("code"), "request_id_conflict")

        # Create a task on wt_b and verify scoped GET /api/supervisor/tasks isolation.
        c_t2, t2 = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {"goal": "Implement scoped task B", "repo_id": repo["id"], "worktree_id": wt_b["id"]},
        )
        self.assertEqual(c_t2, 201)

        _, list_a = srv.request_json(
            "GET",
            "/api/supervisor/tasks?" + urlencode({"repo_id": repo["id"], "worktree_id": wt_a["id"]}),
        )
        self.assertEqual([x["id"] for x in list_a["tasks"]], [t1["id"]])

        _, list_b = srv.request_json(
            "GET",
            "/api/supervisor/tasks?" + urlencode({"repo_id": repo["id"], "worktree_id": wt_b["id"]}),
        )
        self.assertEqual([x["id"] for x in list_b["tasks"]], [t2["id"]])

        # Arbitrary browser transitions are rejected and leave DB state untouched.
        c_tr, b_tr = srv.request_json(
            "POST",
            f"/api/supervisor/tasks/{t1['id']}/transition",
            {"state": "completed", "stage": "completed"},
        )
        self.assertEqual(c_tr, 409)
        self.assertEqual(b_tr.get("code"), "transition_disabled")
        _, t1_after_tr = srv.request_json("GET", f"/api/supervisor/tasks/{t1['id']}")
        self.assertEqual(t1_after_tr["state"], "pending_dispatch")

        # Role overrides: reject tools, location, permission bypass, coordinator, and '-flag' models.
        for bad_roles in (
            {"analyst": {"executor": "claude", "tools": ["read", "write"]}},
            {"implementer": {"executor": "claude", "location": "control_plane"}},
            {"reviewer": {"executor": "claude", "dangerously_skip_permissions": True}},
            {"coordinator": {"executor": "claude"}},
            {"analyst": {"executor": "claude", "model": "--dangerously-skip-permissions"}},
            {"analyst": {"executor": "codex"}},  # unavailable adapter rejected before queueing
        ):
            c_disp_bad, _ = srv.request_json(
                "POST",
                f"/api/supervisor/tasks/{t1['id']}/dispatch",
                {"roles": bad_roles},
            )
            self.assertEqual(c_disp_bad, 400, f"expected 400 for roles={bad_roles}")

        # Deployment start is 409 unconfigured; existing DB record listed honestly; stage mutation disabled.
        c_dep_start, b_dep_start = srv.request_json(
            "POST",
            "/api/supervisor/deployments",
            {"task_id": t1["id"]},
        )
        self.assertEqual(c_dep_start, 409)
        self.assertEqual(b_dep_start.get("code"), "deployment_unconfigured")

        seeded_dep = ags.create_deployment(
            settings,
            task_id=t1["id"],
            source_machine="mac-studio",
            target_machine="",
            build_version="abc1234",
        )
        c_dep_stage, b_dep_stage = srv.request_json(
            "POST",
            f"/api/supervisor/deployments/{seeded_dep['id']}/stage",
            {"stage": "verify", "status": "succeeded"},
        )
        self.assertEqual(c_dep_stage, 409)
        self.assertEqual(b_dep_stage.get("code"), "stage_mutation_disabled")

        c_dep_list, dep_list = srv.request_json(
            "GET",
            f"/api/supervisor/deployments?task_id={t1['id']}",
        )
        self.assertEqual(c_dep_list, 200)
        self.assertEqual(dep_list["count"], 1)
        rec = dep_list["deployments"][0]
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["target_machine"], "")
        self.assertTrue(all(s["status"] == "pending" for s in rec["stages"]))

    # ------------------------------------------------------------------
    # 5. Real 202 dispatch -> worker + fake CLI process execution + provenance
    # ------------------------------------------------------------------

    def test_dispatch_202_runs_real_worker_and_enforces_validation_provenance(self) -> None:
        fake_claude = _write_fake_claude_cli(self.root / "fake-claude-exec")
        os.environ["CONVEYOR_CLAUDE_BIN"] = str(fake_claude)

        # Case A: Operator validation command configured -> task reaches verified 'completed'.
        val_cmd = (
            (
                "verify-feature-txt",
                (
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('feature.txt').read_text().strip() == 'implemented-via-supervisor-worker'",
                ),
            ),
        )
        settings_verified = _make_settings(
            self.root / "exec-verified",
            chat_enabled_flag=False,
            validation_commands=val_cmd,
        )
        srv = self._start_server(settings_verified)

        repo_dir = self.root / "exec-repo"
        _init_git_repo(repo_dir)
        _, repo = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        _, wt = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/worker-run"},
        )

        c_create, created = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "goal": "Create feature.txt in managed worktree",
                "repo_id": repo["id"],
                "worktree_id": wt["id"],
                "acceptance_criteria": "feature.txt contains implemented-via-supervisor-worker",
            },
        )
        self.assertEqual(c_create, 201)
        self.assertEqual(created["state"], "pending_dispatch")

        c_disp, dispatched = srv.request_json(
            "POST",
            f"/api/supervisor/tasks/{created['id']}/dispatch",
            {
                "roles": {
                    "analyst": {"executor": "claude", "model": "claude-sonnet-4-5"},
                    "implementer": {"executor": "claude"},
                    "reviewer": {"executor": "claude"},
                }
            },
        )
        self.assertEqual(c_disp, 202, dispatched)
        self.assertIn(dispatched["state"], ("queued", "dispatching", "analyzing", "implementing"))

        # Idempotent dispatch retry while queued/in-flight returns 202 without duplicate worker/tasks.
        c_disp2, _ = srv.request_json("POST", f"/api/supervisor/tasks/{created['id']}/dispatch", {})
        self.assertEqual(c_disp2, 202)

        detail: dict[str, Any] = {}
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            c_det, detail = srv.request_json("GET", f"/api/supervisor/tasks/{created['id']}")
            self.assertEqual(c_det, 200)
            if detail["state"] in (*ags.TERMINAL_STATES, "waiting_user"):
                break
            time.sleep(0.15)

        self.assertEqual(detail["state"], "completed", detail)
        self.assertIsNotNone(detail["acceptance"])
        self.assertTrue(detail["acceptance"]["passed"])
        self.assertTrue(detail["acceptance"]["provenance_verified"])
        self.assertGreaterEqual(len(detail["handoffs"]), 3)
        roles_ran = {ex["role"] for ex in detail["executions"]}
        self.assertTrue({"analyst", "implementer", "reviewer"}.issubset(roles_ran), roles_ran)
        self.assertIn("feature.txt", detail["metadata"].get("changed_files", []))
        self.assertTrue((Path(wt["path"]) / "feature.txt").is_file())
        self.assertFalse((repo_dir / "feature.txt").exists(), "main worktree must remain untouched")

        # Case B: Without operator validation commands, runtime NEVER fabricates 'completed' -> 'waiting_user'.
        settings_unverified = _make_settings(
            self.root / "exec-unverified",
            chat_enabled_flag=False,
            validation_commands=(),
        )
        srv_unv = self._start_server(settings_unverified)
        repo_dir_b = self.root / "exec-repo-b"
        _init_git_repo(repo_dir_b)
        _, repo_b = srv_unv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir_b)})
        _, wt_b = srv_unv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo_b['id']}/worktrees",
            {"branch": "feat/needs-val"},
        )
        _, task_b = srv_unv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {"goal": "Create feature.txt without operator validation", "repo_id": repo_b["id"], "worktree_id": wt_b["id"]},
        )
        c_db, _ = srv_unv.request_json("POST", f"/api/supervisor/tasks/{task_b['id']}/dispatch", {})
        self.assertEqual(c_db, 202)

        detail_b: dict[str, Any] = {}
        deadline_b = time.monotonic() + 20.0
        while time.monotonic() < deadline_b:
            _, detail_b = srv_unv.request_json("GET", f"/api/supervisor/tasks/{task_b['id']}")
            if detail_b["state"] in (*ags.TERMINAL_STATES, "waiting_user"):
                break
            time.sleep(0.15)

        self.assertEqual(detail_b["state"], "waiting_user", detail_b)
        self.assertEqual(detail_b["stage"], "needs_validation")

    # ------------------------------------------------------------------
    # 6. Cancel terminates live child subprocess & releases worktree lock
    # ------------------------------------------------------------------

    def test_cancel_terminates_live_subprocess_and_releases_lock(self) -> None:
        pid_marker = self.root / "slow-child.pid"
        slow_claude = _write_fake_claude_cli(self.root / "slow-claude", mode="slow", pid_marker=pid_marker)
        os.environ["CONVEYOR_CLAUDE_BIN"] = str(slow_claude)

        settings = _make_settings(self.root / "cancel-env", chat_enabled_flag=False)
        srv = self._start_server(settings)

        repo_dir = self.root / "cancel-repo"
        _init_git_repo(repo_dir)
        _, repo = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        _, wt = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/cancel-me"},
        )
        _, task = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {"goal": "Long running task", "repo_id": repo["id"], "worktree_id": wt["id"]},
        )
        c_disp, _ = srv.request_json("POST", f"/api/supervisor/tasks/{task['id']}/dispatch", {})
        self.assertEqual(c_disp, 202)

        # Wait until subprocess actually spawns and writes its PID.
        wait_spawn = time.monotonic() + 10.0
        while time.monotonic() < wait_spawn and not pid_marker.is_file():
            time.sleep(0.05)
        self.assertTrue(pid_marker.is_file(), "expected fake CLI subprocess to start")
        child_pid = int(pid_marker.read_text(encoding="utf-8").strip())

        c_cancel, cancelled = srv.request_json(
            "POST",
            f"/api/supervisor/tasks/{task['id']}/cancel",
            {"reason": "operator clicked cancel"},
        )
        self.assertEqual(c_cancel, 200)
        self.assertEqual(cancelled["state"], "cancelled")

        # Verify OS process is gone.
        dead = False
        wait_dead = time.monotonic() + 5.0
        while time.monotonic() < wait_dead:
            try:
                os.kill(child_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                dead = True
                break
        self.assertTrue(dead, f"child process {child_pid} should be terminated after cancel")

        # Worktree lock is released so a follow-up task can be created on the same worktree.
        c_next, next_task = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {"goal": "Follow-up after cancel", "repo_id": repo["id"], "worktree_id": wt["id"]},
        )
        self.assertEqual(c_next, 201, next_task)

    # ------------------------------------------------------------------
    # 7. Supervisor-bound read-only chat vs legacy chat routing preserved
    # ------------------------------------------------------------------

    def test_supervisor_bound_chat_read_only_and_legacy_chat_preserved(self) -> None:
        settings = _make_settings(self.root / "chat-env", chat_enabled_flag=True)
        srv = self._start_server(settings)

        repo_dir = self.root / "chat-repo"
        _init_git_repo(repo_dir)
        _, repo = srv.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        _, wt = srv.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/chat-scope"},
        )
        bound_sid = wt["chat_session_id"]

        # Phantom / invented supervisor session IDs are rejected.
        c_phantom_post, _ = srv.request_json(
            "POST",
            "/api/chat",
            {"message": "hi", "session_id": "web:web-console:supwt-wt-phantom999"},
        )
        self.assertEqual(c_phantom_post, 400)
        c_phantom_hist, _ = srv.request_json(
            "GET",
            "/api/chat/history?session_id=web:web-console:supwt-wt-phantom999",
        )
        self.assertEqual(c_phantom_hist, 404)

        # /api/tasks refuses to start legacy Codex jobs on a supervisor worktree session.
        c_legacy_task, b_legacy_task = srv.request_json(
            "POST",
            "/api/tasks",
            {"prompt": "modify files", "session_id": bound_sid, "execution_only": True},
        )
        self.assertEqual(c_legacy_task, 409)
        self.assertEqual(b_legacy_task.get("code"), "supervisor_session_read_only")

        captured_messages: list[list[dict[str, Any]]] = []

        async def _fake_stream_chat(_cfg: Any, messages: list[dict[str, Any]]):
            captured_messages.append(messages)
            yield "建议先在 `feat/chat-scope` 分支新增模块并补充测试。"
            yield "\n[[CONFIDENCE: high]]"

        codex_calls: list[str] = []

        async def _spy_handle_codex_job(msg: Any, port: Any, _runner: Any, *, mode: Any, prompt: str, wait: bool = True):
            codex_calls.append(prompt)
            await port.reply(msg, f"legacy-codex-ran:{prompt}")

        with (
            patch("runner.chat_client.stream_chat", _fake_stream_chat),
            patch("handlers.jobs.handle_codex_job", _spy_handle_codex_job),
        ):
            # Imperative edit request in supervisor-bound chat MUST stay read-only (never calls handle_codex_job).
            for tricky_prompt in (
                "帮我修改 README.md 并运行测试",
                "打开计算器",
                "/goal rewrite everything",
            ):
                st_sse, events = srv.request_sse(
                    "/api/chat",
                    {"message": tricky_prompt, "session_id": bound_sid},
                )
                self.assertEqual(st_sse, 200)
                assert isinstance(events, list)
                event_names = [e[0] for e in events]
                self.assertIn("done", event_names)

            self.assertEqual(codex_calls, [], "supervisor chat must NEVER auto-start Codex/Pi jobs")
            self.assertEqual(len(captured_messages), 3)
            sys_prompt = captured_messages[0][0]["content"]
            self.assertIn(repo["name"], sys_prompt)
            self.assertIn(wt["path"], sys_prompt)
            self.assertIn("feat/chat-scope", sys_prompt)

            # History is persisted under the bound worktree session.
            c_hist, hist_body = srv.request_json(
                "GET",
                "/api/chat/history?" + urlencode({"session_id": bound_sid}),
            )
            self.assertEqual(c_hist, 200)
            self.assertEqual(len(hist_body["messages"]), 6)
            # First turn was an edit imperative, so it carries a reviewable task_suggestion.
            self.assertEqual(
                hist_body["messages"][1]["task_suggestion"]["prompt"],
                "帮我修改 README.md 并运行测试",
            )

            # Meanwhile, a legacy non-supervisor web chat session on the SAME server preserves existing routing!
            st_leg, leg_events = srv.request_sse(
                "/api/chat",
                {"message": "帮我修改 README.md 并运行测试", "session_id": "webchat-legacy001"},
            )
            self.assertEqual(st_leg, 200)
            assert isinstance(leg_events, list)
            self.assertEqual(codex_calls, ["帮我修改 README.md 并运行测试"])

        # When main chat provider errors at runtime, supervisor chat reports unavailable honestly
        # and NEVER falls back to runner / handle_codex_job.
        from runner.chat_client import ChatError

        async def _failing_stream_chat(_cfg: Any, _messages: list[dict[str, Any]]):
            raise ChatError("upstream 503 sk-secret12345678901234567890")
            yield ""  # pragma: no cover

        codex_calls.clear()
        with (
            patch("runner.chat_client.stream_chat", _failing_stream_chat),
            patch("handlers.jobs.handle_codex_job", _spy_handle_codex_job),
        ):
            st_err, err_events = srv.request_sse(
                "/api/chat",
                {"message": "帮我修改 README.md", "session_id": bound_sid},
            )
            self.assertEqual(st_err, 200)
            assert isinstance(err_events, list)
            self.assertEqual(codex_calls, [])
            done_payloads = [d for ev, d in err_events if ev == "done"]
            self.assertEqual(done_payloads, [{"outcome": "unavailable"}])
            err_payloads = [d for ev, d in err_events if ev == "error"]
            self.assertTrue(err_payloads)
            self.assertNotIn("sk-secret12345678901234567890", json.dumps(err_payloads))

        # Bound input sizes: oversized goal (>8000 chars) rejected with 400.
        c_huge, _ = srv.request_json(
            "POST",
            "/api/supervisor/tasks",
            {"goal": "x" * 8001, "repo_id": repo["id"], "worktree_id": wt["id"]},
        )
        self.assertEqual(c_huge, 400)

        # Honest unavailable when chat model is disabled (even for /goal on supervisor session).
        no_chat_settings = dataclasses.replace(
            settings,
            chat_mode="off",
            chat_base_url=None,
            chat_api_key=None,
            chat_model=None,
        )
        srv.control.settings = no_chat_settings
        c_off_chat, b_off_chat = srv.request_json(
            "POST",
            "/api/chat",
            {"message": "/goal try bypass", "session_id": bound_sid},
        )
        self.assertEqual(c_off_chat, 409)
        self.assertEqual(b_off_chat.get("code"), "chat_disabled")


if __name__ == "__main__":
    unittest.main()
