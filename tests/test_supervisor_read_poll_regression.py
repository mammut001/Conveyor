"""tests/test_supervisor_read_poll_regression.py — Focused regression tests for
worktree transcript session read-polling and discussion snapshot kind filtering.

Addresses:
- parent-read-poll-archive-report.json: GET /api/supervisor/repositories polling
  must not reactivate archived transcript sessions or advance updated_at.
- Snapshot kind filtering: exclude internal records (tool_approval, tool_result,
  status, approval_prompt, internal) BEFORE latest-N selection and counting.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from config import Settings
from handlers.job_queue import JobQueue
from conveyor.agents.supervisor_service import (
    DISCUSSION_KINDS,
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_TURNS,
    capture_discussion_snapshot,
    create_managed_worktree,
    create_scoped_task,
    register_repository,
)
from conveyor.core.transcript_store import get_transcript_store
from web_console import WebConsoleHandler, WebConsoleServer
from conveyor.web.web_control import WebControl


TOKEN = "test-token-poll-archive-regression-9876543210"


def _make_settings(root: Path) -> Settings:
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
        chat_mode="off",
        chat_base_url=None,
        chat_api_key=None,
        chat_model=None,
        chat_tools_enabled=False,
        agent_supervisor_enabled=True,
        agent_supervisor_analyst_executor="echo",
        agent_supervisor_implementer_executor="echo",
        agent_supervisor_reviewer_executor="echo",
        agent_supervisor_max_repair_rounds=1,
        agent_supervisor_task_timeout_seconds=25,
        agent_supervisor_validation_commands=(),
    )


def _init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Regression Test"], check=True)
    (repo_dir / "README.md").write_text("# Regression Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class FakeRunner:
    name = "codex"


class _HttpHarness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runner = FakeRunner()
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
    ) -> tuple[int, dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            }
            data = json.dumps(body) if body is not None else None
            conn.request(method, path, body=data, headers=headers)
            res = conn.getresponse()
            raw = res.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"_raw": raw}
            return res.status, payload
        finally:
            conn.close()


class SupervisorReadPollArchiveRegressionTests(unittest.TestCase):
    """End-to-end HTTP regression testing parent-read-poll-archive-report.json."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repos" / "isolated-repo"
        self.head_sha = _init_git_repo(self.repo_dir)
        self.harness = _HttpHarness(self.settings)

    def tearDown(self) -> None:
        self.harness.close()
        self.temp_dir.cleanup()

    def test_http_get_repositories_poll_preserves_archived_session(self) -> None:
        # 1. Register isolated repo via HTTP
        c_repo, repo = self.harness.request_json(
            "POST",
            "/api/supervisor/repositories",
            {"path": str(self.repo_dir), "name": "isolated-repo"},
        )
        self.assertEqual(c_repo, 201)
        repo_id = repo["id"]

        # 2. Main worktree session is created
        worktrees = repo.get("worktrees", [])
        self.assertTrue(len(worktrees) >= 1)
        main_wt = worktrees[0]
        chat_sid = main_wt["chat_session_id"]
        self.assertTrue(chat_sid)

        store = get_transcript_store(self.settings)
        sess = store.get_session(chat_sid)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["archived"], 0)

        # 3. Create managed worktree to test branch worktree session as well
        c_wt, wt = self.harness.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo_id}/worktrees",
            {"branch": "feature/survey"},
        )
        self.assertEqual(c_wt, 201)
        wt_chat_sid = wt["chat_session_id"]

        # Append messages to both sessions so they have distinct timestamps and titles
        store.append(chat_sid, "user", "Main worktree discussion", kind="chat")
        store.append(wt_chat_sid, "user", "Branch worktree discussion", kind="chat")

        # 4. Archive both sessions
        self.assertTrue(store.archive_session(chat_sid))
        self.assertTrue(store.archive_session(wt_chat_sid))

        archived_main = store.get_session(chat_sid)
        archived_wt = store.get_session(wt_chat_sid)
        self.assertEqual(archived_main["archived"], 1)
        self.assertEqual(archived_wt["archived"], 1)

        saved_main_updated_at = archived_main["updated_at"]
        saved_main_title = archived_main["title"]
        saved_wt_updated_at = archived_wt["updated_at"]
        saved_wt_title = archived_wt["title"]

        time.sleep(0.05)

        # 5. Routine Code polling: GET /api/supervisor/repositories repeatedly
        for i in range(5):
            c_list, body = self.harness.request_json("GET", "/api/supervisor/repositories")
            self.assertEqual(c_list, 200)
            self.assertIn("repositories", body)
            self.assertTrue(isinstance(body["repositories"], list))

        # 6. Verify archived=1, updated_at, and title are strictly untouched
        poll_main = store.get_session(chat_sid)
        poll_wt = store.get_session(wt_chat_sid)

        self.assertEqual(poll_main["archived"], 1, "poll must not reset archived to 0")
        self.assertEqual(poll_main["updated_at"], saved_main_updated_at, "poll must not change updated_at")
        self.assertEqual(poll_main["title"], saved_main_title, "poll must not touch title")

        self.assertEqual(poll_wt["archived"], 1, "poll must not reset worktree session archived")
        self.assertEqual(poll_wt["updated_at"], saved_wt_updated_at, "poll must not change worktree updated_at")
        self.assertEqual(poll_wt["title"], saved_wt_title, "poll must not touch worktree title")

        # 7. Actual new transcript append MUST reactivate and advance updated_at
        time.sleep(0.05)
        new_msg = store.append(chat_sid, "user", "Actual chat message arrives", kind="chat")

        reactivated_main = store.get_session(chat_sid)
        self.assertEqual(reactivated_main["archived"], 0, "actual append must reactivate session")
        self.assertGreater(reactivated_main["updated_at"], saved_main_updated_at, "actual append must update timestamp")

        # wt_chat_sid should remain archived since it received no new messages
        wt_still_archived = store.get_session(wt_chat_sid)
        self.assertEqual(wt_still_archived["archived"], 1)
        self.assertEqual(wt_still_archived["updated_at"], saved_wt_updated_at)


class DiscussionSnapshotKindFilteringRegressionTests(unittest.TestCase):
    """Verify non-discussion kinds are filtered BEFORE latest-N selection/counting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repos" / "kind-filter-repo"
        _init_git_repo(self.repo_dir)
        self.repo, _ = register_repository(self.settings, path=str(self.repo_dir))
        self.wt = create_managed_worktree(
            self.settings,
            repo_id=self.repo["id"],
            branch="feat/kind-filter-test",
        )
        self.store = get_transcript_store(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_snapshot_excludes_internal_kinds_before_latest_n_and_preserves_tool_text(self) -> None:
        sid = self.wt["chat_session_id"]

        # Append 5 legitimate discussion turns (legacy None, 'chat', 'task_suggestion')
        # including user text mentioning 'tool'
        self.store.append(sid, "user", "Please use the migration tool for SQLite schema", kind="chat")
        self.store.append(sid, "assistant", "I will run the migration tool now.", kind="chat")
        self.store.append(sid, "user", "Also check index performance with tool explain")  # kind=None
        self.store.append(sid, "assistant", "Indices are verified optimal.", kind="chat")
        self.store.append(sid, "assistant", "Suggest adding composite index on session_id, created_at", kind="task_suggestion")

        # Now append 12 assistant status/approval/tool records (greater than MAX_SNAPSHOT_TURNS 8)
        non_discussion_records = [
            ("tool_approval", "Approve execution of tool git status?"),
            ("tool_result", "On branch feat/kind-filter-test"),
            ("status", "Running preflight checks..."),
            ("approval_prompt", "Authorize database migration?"),
            ("internal", "Task loop iteration 1"),
            ("tool_approval", "Approve execution of tool migration?"),
            ("tool_result", "Migration applied successfully"),
            ("status", "Analyzing test failures..."),
            ("approval_prompt", "Authorize unit test runner?"),
            ("tool_approval", "Approve pytest?"),
            ("tool_result", "Ran 15 tests: OK"),
            ("internal", "Snapshot cache checkpoint"),
        ]
        for kind, content in non_discussion_records:
            self.store.append(sid, "assistant", content, kind=kind)

        # Session has 5 discussion + 12 internal = 17 total messages
        self.assertEqual(self.store.count_messages(sid), 17)

        # Capture snapshot
        snap = capture_discussion_snapshot(self.settings, sid, max_turns=8)

        # All 5 discussion messages must be present; ZERO of the 12 non-discussion records
        self.assertEqual(len(snap["messages"]), 5)
        self.assertFalse(snap["truncated"])

        # Validate message contents and order (chronological ASC)
        msg_texts = [m["text"] for m in snap["messages"]]
        self.assertEqual(msg_texts[0], "Please use the migration tool for SQLite schema")
        self.assertEqual(msg_texts[1], "I will run the migration tool now.")
        self.assertEqual(msg_texts[2], "Also check index performance with tool explain")
        self.assertEqual(msg_texts[3], "Indices are verified optimal.")
        self.assertEqual(msg_texts[4], "Suggest adding composite index on session_id, created_at")

        # Crucial check: user messages mentioning 'tool' were NOT dropped
        self.assertIn("tool", msg_texts[0])
        self.assertIn("tool", msg_texts[2])

        # Validate canonical digest contract
        serialized = json.dumps(
            snap["messages"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(snap["sha256"], hashlib.sha256(serialized).hexdigest())

        # Now test that latest-N selection happens AFTER kind filtering:
        # Add 5 more discussion turns, making 10 discussion turns total (with 12 internal turns in between)
        for i in range(5):
            self.store.append(sid, "user" if i % 2 == 0 else "assistant", f"Followup discussion turn {i}", kind="chat")

        snap_truncated = capture_discussion_snapshot(self.settings, sid, max_turns=8)
        self.assertEqual(len(snap_truncated["messages"]), 8)
        self.assertTrue(snap_truncated["truncated"])

        # Should retain the latest 8 of the 10 eligible discussion turns
        # Oldest 2 ("Please use the migration tool..." and "I will run the migration tool...") dropped
        self.assertEqual(snap_truncated["messages"][0]["text"], "Also check index performance with tool explain")
        self.assertEqual(snap_truncated["messages"][-1]["text"], "Followup discussion turn 4")


if __name__ == "__main__":
    unittest.main()
