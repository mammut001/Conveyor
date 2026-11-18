"""tests/test_supervisor_archive_projects.py — P1: archive projects (hide without deleting).

Covers:
1. Schema migration (`archived_at` column added to existing DBs) and default/include_archived listing.
2. `POST /api/supervisor/repositories/<id>/archive` and `.../unarchive`:
   - Preserves repository on disk, branches, worktrees, and goal history.
   - Refuses archiving (409) when the repo has a running/waiting goal or active task,
     with 「这个项目还有正在跑的目标或任务，先停下再归档。」.
   - Allows archiving once the goal/task stops or completes.
   - Same auth + Origin / X-Requested-With pattern as other supervisor write routes.
   - Refuses starting a new task or goal on an archived repo.
3. Chat intents ignore archived repos (「在哪个项目上干？」 choices, single-project default,
   explicit name / recent_repo_id fallback, resume intent).
"""
from __future__ import annotations

import asyncio
import http.client
import json
import sqlite3
import subprocess
import tempfile
import types
import unittest
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch

from channel.types import InboundMessage
from conveyor.agents import agent_supervisor as ags
from conveyor.agents import chat_intents
from conveyor.agents import goal_notify
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_service as svc
from conveyor.agents.intent_decider import Decision
from tests.test_chat_intents import _MemoryPort, _start_available_executor_patches
from tests.test_supervisor_api import TOKEN, _HttpHarness, _make_settings


def _git(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Supervisor Test")
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "initial commit")


class _FakeHandler:
    """Minimal WebConsoleHandler stand-in for direct route dispatch tests."""

    def __init__(self, settings: Any, headers: dict[str, str] | None = None) -> None:
        self.server = types.SimpleNamespace(
            control=types.SimpleNamespace(settings=settings),
            loop=None,
        )
        self.headers = headers if headers is not None else {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        self.response: tuple[int, dict[str, Any]] | None = None

    def _json(self, status: Any, payload: dict[str, Any]) -> None:
        self.response = (int(status), payload)


class ArchiveServiceAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-archive-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(goal_notify.drain)
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "probe-repo"
        _init_repo(self.repo_dir)
        repo, _ = svc.register_repository(self.settings, path=str(self.repo_dir), name="Handoff探针1114")
        self.repo_id = str(repo["id"])
        self.wt = svc.create_managed_worktree(self.settings, self.repo_id, branch="sup/probe-wt")
        self.wt_id = str(self.wt["id"])

    def _post(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_post

        handler = _FakeHandler(self.settings, headers=headers)
        handle_post(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        return handler.response

    def _get(self, path: str, query: dict[str, list[str]] | None = None) -> tuple[int, dict[str, Any]]:
        from conveyor.web.web_supervisor_api import handle_get

        handler = _FakeHandler(self.settings)
        handle_get(handler, path, path.strip("/").split("/"), query or {})
        assert handler.response is not None
        return handler.response

    def test_schema_migration_adds_archived_at_column(self) -> None:
        legacy_root = self.root / "legacy"
        legacy_settings = _make_settings(legacy_root)
        db_path = svc._db_path(legacy_settings)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE supervisor_repositories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    canonical_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE supervisor_worktrees (
                    id TEXT PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    canonical_path TEXT NOT NULL UNIQUE,
                    managed INTEGER NOT NULL DEFAULT 0,
                    chat_session_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        with svc._SCHEMA_INIT_LOCK:
            svc._INITIALIZED_DBS.discard(str(db_path))

        repos = svc.list_repositories(legacy_settings)
        self.assertEqual(repos, [])
        check_conn = sqlite3.connect(str(db_path))
        try:
            cols = {str(r[1]) for r in check_conn.execute("PRAGMA table_info(supervisor_repositories)").fetchall()}
        finally:
            check_conn.close()
        self.assertIn("archived_at", cols)

    def test_archive_unarchive_and_include_archived_listing_preserves_data(self) -> None:
        # Before archive: listed with archived_at=None.
        code, body = self._get("/api/supervisor/repositories")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["repositories"]), 1)
        self.assertIsNone(body["repositories"][0]["archived_at"])

        # Create a finished goal to verify goal history is preserved.
        goal = goals_mod.create_goal(
            None,
            self.settings,
            {"repo_id": self.repo_id, "worktree_id": self.wt_id, "outcome": "先跑完一个目标"},
        )
        goals_mod.stop_goal(None, self.settings, goal["id"], post_report=False)

        # Archive -> returns updated repo with archived_at.
        a_code, a_body = self._post(f"/api/supervisor/repositories/{self.repo_id}/archive")
        self.assertEqual(a_code, 200, a_body)
        self.assertTrue(a_body["archived_at"])

        # Default listing hides archived repo.
        code_default, body_default = self._get("/api/supervisor/repositories")
        self.assertEqual(code_default, 200)
        self.assertEqual(body_default["repositories"], [])

        # include_archived=1 returns it with archived_at.
        code_inc, body_inc = self._get("/api/supervisor/repositories", {"include_archived": ["1"]})
        self.assertEqual(code_inc, 200)
        self.assertEqual(len(body_inc["repositories"]), 1)
        self.assertEqual(body_inc["repositories"][0]["id"], self.repo_id)
        self.assertTrue(body_inc["repositories"][0]["archived_at"])

        # Repository folder, worktree folder, branch, and goal history remain intact.
        self.assertTrue(self.repo_dir.is_dir())
        self.assertTrue(Path(self.wt["path"]).is_dir())
        branches = _git(self.repo_dir, "branch", "--list", "sup/probe-wt").strip()
        self.assertIn("sup/probe-wt", branches)
        self.assertEqual(len(goals_mod.list_goals(self.settings)), 1)

        # Cannot create a new task or goal while archived.
        with self.assertRaises(svc.SupervisorServiceError) as task_exc:
            svc.create_scoped_task(
                self.settings,
                {"repo_id": self.repo_id, "worktree_id": self.wt_id, "goal": "试一下"},
            )
        self.assertEqual(task_exc.exception.status, 409)
        self.assertEqual(task_exc.exception.code, "repository_archived")

        with self.assertRaises(svc.SupervisorServiceError) as goal_exc:
            goals_mod.create_goal(
                None,
                self.settings,
                {"repo_id": self.repo_id, "worktree_id": self.wt_id, "outcome": "试一下"},
            )
        self.assertEqual(goal_exc.exception.status, 409)
        self.assertEqual(goal_exc.exception.code, "repository_archived")

        # Unarchive -> clears archived_at and restores to default listing.
        u_code, u_body = self._post(f"/api/supervisor/repositories/{self.repo_id}/unarchive")
        self.assertEqual(u_code, 200, u_body)
        self.assertIsNone(u_body["archived_at"])
        code_after, body_after = self._get("/api/supervisor/repositories")
        self.assertEqual(code_after, 200)
        self.assertEqual(len(body_after["repositories"]), 1)
        self.assertIsNone(body_after["repositories"][0]["archived_at"])

    def test_refuses_archiving_while_goal_running_or_task_active(self) -> None:
        # 1. Running goal -> 409 with exact Chinese message.
        goal = goals_mod.create_goal(
            None,
            self.settings,
            {"repo_id": self.repo_id, "worktree_id": self.wt_id, "outcome": "持续优化"},
        )
        code, body = self._post(f"/api/supervisor/repositories/{self.repo_id}/archive")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error"], "这个项目还有正在跑的目标或任务，先停下再归档。")
        self.assertEqual(body["code"], "repository_busy")

        # Stop goal -> now archiving succeeds.
        goals_mod.stop_goal(None, self.settings, goal["id"], post_report=False)
        ok_code, ok_body = self._post(f"/api/supervisor/repositories/{self.repo_id}/archive")
        self.assertEqual(ok_code, 200, ok_body)
        self._post(f"/api/supervisor/repositories/{self.repo_id}/unarchive")

        # 2. Active task -> 409 with exact Chinese message.
        task, _ = svc.create_scoped_task(
            self.settings,
            {"repo_id": self.repo_id, "worktree_id": self.wt_id, "goal": "修 bug"},
        )
        ags.dispatch_task(
            self.settings,
            task["id"],
            worktree_path=self.wt["path"],
            branch="sup/probe-wt",
            base_sha=self.wt["head"],
        )
        code2, body2 = self._post(f"/api/supervisor/repositories/{self.repo_id}/archive")
        self.assertEqual(code2, 409, body2)
        self.assertEqual(body2["error"], "这个项目还有正在跑的目标或任务，先停下再归档。")

        # Cancel active task -> now archiving succeeds.
        svc.cancel_scoped_task(None, self.settings, task["id"], reason="测试停止")
        ok_code2, _ = self._post(f"/api/supervisor/repositories/{self.repo_id}/archive")
        self.assertEqual(ok_code2, 200)

    def test_archive_routes_enforce_origin_feature_flag_404_and_http_auth(self) -> None:
        # Missing X-Requested-With / cross-site headers -> 403 origin_or_request_denied.
        for action in ("archive", "unarchive"):
            code, body = self._post(f"/api/supervisor/repositories/{self.repo_id}/{action}", headers={})
            self.assertEqual(code, 403)
            self.assertEqual(body.get("code"), "origin_or_request_denied")

        # Unknown repo -> 404.
        code_404, body_404 = self._post("/api/supervisor/repositories/repo-missing/archive")
        self.assertEqual(code_404, 404)
        self.assertEqual(body_404.get("code"), "repository_not_found")

        # Disabled feature -> 409.
        off = replace(self.settings, agent_supervisor_enabled=False)
        from conveyor.web.web_supervisor_api import handle_post

        handler = _FakeHandler(off)
        path = f"/api/supervisor/repositories/{self.repo_id}/archive"
        handle_post(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        self.assertEqual(handler.response[0], 409)
        self.assertEqual(handler.response[1].get("code"), "agent_supervisor_disabled")

        # Real HTTP server roundtrip: unauthenticated -> 401; authenticated + same-site -> 200.
        harness = _HttpHarness(self.settings)
        self.addCleanup(harness.close)

        conn = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
        try:
            conn.request(
                "POST",
                f"/api/supervisor/repositories/{self.repo_id}/archive",
                body=b"{}",
                headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
            )
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, HTTPStatus.UNAUTHORIZED)
        finally:
            conn.close()

        conn2 = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
        try:
            conn2.request(
                "POST",
                f"/api/supervisor/repositories/{self.repo_id}/archive",
                body=b"{}",
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            resp2 = conn2.getresponse()
            payload = json.loads(resp2.read().decode("utf-8"))
            self.assertEqual(resp2.status, HTTPStatus.OK)
            self.assertTrue(payload["archived_at"])
        finally:
            conn2.close()


class ChatIntentsIgnoreArchivedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-ci-archive-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(goal_notify.drain)
        self.settings = _make_settings(Path(self._tmp.name))
        self.session_id = "web:web-console:archive-intent-session"
        _start_available_executor_patches(self)

    def _send(self, text: str) -> _MemoryPort:
        port = _MemoryPort(self.settings, self.session_id)
        msg = InboundMessage(
            channel="web",
            operator_id="web-console",
            chat_id="archive-intent-session",
            message_id="m1",
            text=text,
            chat_type="p2p",
        )
        handled = asyncio.run(chat_intents.maybe_handle_web_chat(msg, port, self.settings, text))
        self.assertTrue(handled)
        return port

    def test_project_resolution_picker_and_single_default_ignore_archived(self) -> None:
        p1 = svc.create_managed_project(self.settings, name="real-app")["repository"]
        p2 = svc.create_managed_project(self.settings, name="probe-1114")["repository"]
        p3 = svc.create_managed_project(self.settings, name="qa-live-1115")["repository"]

        # Archive probe-1114 -> picker only offers real-app and qa-live-1115.
        svc.archive_repository(self.settings, p2["id"])
        self.session_id = "web:web-console:fresh-picker-1"
        port = self._send("持续改进两小时，修好登录")
        payload = port.events[0][1]
        self.assertIn("在哪个项目上干？", payload["text"])
        labels = {b["label"] for b in payload["action_card"]["buttons"]}
        self.assertEqual(labels, {"real-app", "qa-live-1115"})

        # Explicit mention of archived repo does not match it.
        explicit_port = self._send("在 probe-1114 上持续改进 2 小时，修好登录")
        explicit_payload = explicit_port.events[0][1]
        self.assertIn("没有找到叫「probe-1114」的项目", explicit_payload["text"])
        self.assertEqual(
            {b["label"] for b in explicit_payload["action_card"]["buttons"]},
            {"real-app", "qa-live-1115"},
        )

        # Even if recent_repo_id pointed to qa-live-1115, archiving it leaves only real-app -> single-project default!
        chat_intents._set_recent_repo(self.settings, "web:web-console:recent-archived", p3["id"])
        svc.archive_repository(self.settings, p3["id"])
        self.session_id = "web:web-console:recent-archived"
        single_port = self._send("持续改进两小时，整理测试")
        single_payload = single_port.events[0][1]
        self.assertIn("准备在 real-app 上持续改进 2 小时", single_payload["text"])
        self.assertNotIn("在哪个项目上干？", single_payload["text"])

        # Archiving the last active repo -> says there are no projects yet.
        svc.archive_repository(self.settings, p1["id"])
        self.session_id = "web:web-console:none-left"
        empty_port = self._send("持续改进两小时，整理测试")
        self.assertIn("目前还没有项目", empty_port.events[0][1]["text"])

        # Unarchiving p1 brings it back for intents.
        svc.unarchive_repository(self.settings, p1["id"])
        restored_port = self._send("持续改进两小时，整理测试")
        self.assertIn("准备在 real-app 上持续改进 2 小时", restored_port.events[0][1]["text"])

    def test_resume_intent_skips_archived_repo_goals(self) -> None:
        real = svc.create_managed_project(self.settings, name="real-app")
        probe = svc.create_managed_project(self.settings, name="probe-1114")

        g_real = goals_mod.create_goal(
            None,
            self.settings,
            {
                "repo_id": real["repository"]["id"],
                "worktree_id": real["worktree"]["id"],
                "outcome": "完善主页",
            },
        )
        goals_mod.stop_goal(None, self.settings, g_real["id"], post_report=False)

        g_probe = goals_mod.create_goal(
            None,
            self.settings,
            {
                "repo_id": probe["repository"]["id"],
                "worktree_id": probe["worktree"]["id"],
                "outcome": "探针任务",
            },
        )
        goals_mod.stop_goal(None, self.settings, g_probe["id"], post_report=False)

        # Archive probe-1114 -> resume intent skips probe-1114's newer goal and offers real-app.
        svc.archive_repository(self.settings, probe["repository"]["id"])
        with patch.object(
            chat_intents.intent_decider,
            "decide_intent",
            return_value=Decision("resume", 0.91, {"resume": 0.91}, "gemini", 105),
        ):
            port = self._send("继续之前的目标")
        payload = port.events[0][1]
        self.assertIn("real-app", payload["text"])
        self.assertNotIn("probe-1114", payload["text"])


if __name__ == "__main__":
    unittest.main()
