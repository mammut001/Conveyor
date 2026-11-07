"""tests/test_supervisor_discussion_snapshot.py — Focused tests for supervisor task
discussion snapshot capture, metadata contract, isolation, and idempotency.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agent_supervisor as ags
from config import Settings
from handlers.job_queue import JobQueue
from supervisor_service import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_TURNS,
    SupervisorServiceError,
    capture_discussion_snapshot,
    create_managed_worktree,
    create_scoped_task,
    get_scoped_task,
    register_repository,
)
from transcript_store import get_transcript_store
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


TOKEN = "test-token-discussion-snapshot-1234567890"


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
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Supervisor Test"], check=True)
    (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
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
            status = res.status
            try:
                return status, json.loads(raw)
            except Exception:
                return status, {"raw": raw}
        finally:
            conn.close()


class SupervisorDiscussionSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_git_repo(self.repo_dir)

        # Register repository through supervisor service
        self.repo, _ = register_repository(self.settings, path=str(self.repo_dir))
        self.store = get_transcript_store(self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_worktree(self, branch: str) -> dict[str, Any]:
        return create_managed_worktree(self.settings, self.repo["id"], branch=branch)

    def test_discussion_snapshot_captured_and_conforms_to_contract(self) -> None:
        """Task creation must capture discussion snapshot into metadata conforming
        exactly to the API-to-core contract while keeping task.goal concise.
        """
        wt = self._create_worktree("feat/snapshot-contract")
        session_id = wt["chat_session_id"]

        # Populate a discussion: user -> assistant -> user -> assistant
        self.store.append(session_id, "user", "How should we implement the metrics collector?")
        self.store.append(session_id, "assistant", "We can use an in-memory ring buffer with Prometheus export.")
        self.store.append(session_id, "user", "Let's proceed with the ring buffer approach.")
        self.store.append(session_id, "assistant", "Agreed, ring buffer sized to 1024 entries.")

        concise_goal = "Implement ring buffer metrics collector"
        acceptance = "Ensure buffer does not exceed 1024 entries"
        task_data, created = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": concise_goal,
            "acceptance_criteria": acceptance,
        })
        self.assertTrue(created)
        self.assertEqual(task_data["goal"], concise_goal)

        meta = task_data.get("metadata", {})
        self.assertIn("discussion_snapshot", meta)
        snapshot = meta["discussion_snapshot"]

        # Verify contract keys
        self.assertEqual(set(snapshot.keys()), {"session_id", "captured_at", "messages", "sha256", "truncated"})
        self.assertEqual(snapshot["session_id"], session_id)
        self.assertFalse(snapshot["truncated"])
        self.assertEqual(len(snapshot["messages"]), 4)

        # Verify message structure
        for m in snapshot["messages"]:
            self.assertEqual(set(m.keys()), {"id", "role", "text"})
            self.assertIn(m["role"], ("user", "assistant"))

        # Verify exact sha256 formula
        expected_sha = hashlib.sha256(
            json.dumps(snapshot["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(snapshot["sha256"], expected_sha)

        # Persistence check: get_scoped_task must return the exact same snapshot
        persisted = get_scoped_task(self.settings, task_data["id"])
        self.assertEqual(persisted["metadata"]["discussion_snapshot"], snapshot)

    def test_distinct_sessions_do_not_leak(self) -> None:
        """Tasks created on different worktrees must capture strictly their own
        worktree's discussion session without leakage.
        """
        wt_a = self._create_worktree("feat/session-a")
        wt_b = self._create_worktree("feat/session-b")

        sid_a = wt_a["chat_session_id"]
        sid_b = wt_b["chat_session_id"]
        self.assertNotEqual(sid_a, sid_b)

        self.store.append(sid_a, "user", "Plan A: use PostgreSQL")
        self.store.append(sid_a, "assistant", "PostgreSQL schema created")

        self.store.append(sid_b, "user", "Plan B: use SQLite exclusively")
        self.store.append(sid_b, "assistant", "SQLite schema created")

        task_a, _ = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt_a["id"],
            "goal": "Implement database backend A",
        })
        task_b, _ = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt_b["id"],
            "goal": "Implement database backend B",
        })

        snap_a = task_a["metadata"]["discussion_snapshot"]
        snap_b = task_b["metadata"]["discussion_snapshot"]

        texts_a = [m["text"] for m in snap_a["messages"]]
        texts_b = [m["text"] for m in snap_b["messages"]]

        self.assertIn("Plan A: use PostgreSQL", texts_a)
        self.assertNotIn("Plan B: use SQLite exclusively", texts_a)

        self.assertIn("Plan B: use SQLite exclusively", texts_b)
        self.assertNotIn("Plan A: use PostgreSQL", texts_b)

    def test_empty_session_allowed(self) -> None:
        """A worktree session with zero discussion turns must succeed with an empty
        message list, truncated=False, and sha256 of empty list.
        """
        wt = self._create_worktree("feat/empty-chat")
        task_data, created = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Task without prior chat",
        })
        self.assertTrue(created)
        snap = task_data["metadata"]["discussion_snapshot"]
        self.assertEqual(snap["messages"], [])
        self.assertFalse(snap["truncated"])
        expected_empty_sha = hashlib.sha256(b"[]").hexdigest()
        self.assertEqual(snap["sha256"], expected_empty_sha)

    def test_long_history_captures_latest_not_oldest_and_stays_within_bound(self) -> None:
        """When history exceeds MAX_SNAPSHOT_TURNS (8), snapshot must capture the newest
        turns in chronological order (not oldest first) and accurately mark truncated=True.
        """
        wt = self._create_worktree("feat/long-history")
        sid = wt["chat_session_id"]

        for i in range(1, 21):
            role = "user" if i % 2 == 1 else "assistant"
            self.store.append(sid, role, f"Turn number {i:02d} discussion content")

        task_data, _ = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Task with long prior chat",
        })
        snap = task_data["metadata"]["discussion_snapshot"]
        self.assertTrue(snap["truncated"], "long history must be marked truncated")
        self.assertEqual(len(snap["messages"]), MAX_SNAPSHOT_TURNS)

        # Messages must be the latest 8 (turns 13 through 20) in chronological order
        texts = [m["text"] for m in snap["messages"]]
        self.assertEqual(texts[0], "Turn number 13 discussion content")
        self.assertEqual(texts[-1], "Turn number 20 discussion content")
        self.assertNotIn("Turn number 01 discussion content", texts)

        # Verify serialized size is within bounds
        serialized = json.dumps(snap["messages"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(serialized), MAX_SNAPSHOT_BYTES)

    def test_large_message_content_is_bounded_to_budget(self) -> None:
        """Very large messages must be bounded within MAX_SNAPSHOT_BYTES (~16KiB)
        and marked truncated=True.
        """
        wt = self._create_worktree("feat/huge-msg")
        sid = wt["chat_session_id"]

        # Two messages totaling > 20 KiB, exceeding 16 KiB limit
        self.store.append(sid, "user", "A" * 12000)
        self.store.append(sid, "assistant", "B" * 12000)

        task_data, _ = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Analyze huge spec",
        })
        snap = task_data["metadata"]["discussion_snapshot"]
        self.assertTrue(snap["truncated"])
        serialized = json.dumps(snap["messages"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(serialized), MAX_SNAPSHOT_BYTES)

    def test_idempotent_retry_preserves_original_snapshot_and_digest(self) -> None:
        """Idempotent retry with request_id must return the original task metadata
        and sha256 even if new chat messages arrive after creation.
        """
        wt = self._create_worktree("feat/idempotent-chat")
        sid = wt["chat_session_id"]
        req_id = "req-idempotent-snapshot-001"

        self.store.append(sid, "user", "Initial proposal")
        self.store.append(sid, "assistant", "Proposal acknowledged")

        task1, created1 = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Perform proposal work",
            "request_id": req_id,
        })
        self.assertTrue(created1)
        snap1 = task1["metadata"]["discussion_snapshot"]

        # Later chat arrives in the same session
        self.store.append(sid, "user", "Wait, here is a later thought!")
        self.store.append(sid, "assistant", "Later thought reply.")

        # Idempotent retry with the same request_id
        task2, created2 = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Perform proposal work",
            "request_id": req_id,
        })
        self.assertFalse(created2)
        snap2 = task2["metadata"]["discussion_snapshot"]

        # Must return the exact original snapshot, NOT mutated or recaptured
        self.assertEqual(snap1["sha256"], snap2["sha256"])
        self.assertEqual(snap1["captured_at"], snap2["captured_at"])
        self.assertEqual(len(snap2["messages"]), 2)
        texts = [m["text"] for m in snap2["messages"]]
        self.assertNotIn("Wait, here is a later thought!", texts)

    def test_client_supplied_snapshot_cannot_replace_server_history(self) -> None:
        """Client sending fake/malformed discussion_snapshot in request body must NOT
        replace server history. Snapshot is always captured from transcript store.
        """
        wt = self._create_worktree("feat/no-client-spoof")
        sid = wt["chat_session_id"]

        self.store.append(sid, "user", "Real server-stored turn")
        self.store.append(sid, "assistant", "Real server-stored answer")

        malicious_body = {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Do not let client spoof context",
            "discussion_snapshot": {
                "session_id": "fake-session",
                "messages": [{"id": "fake", "role": "user", "text": "Spoofed prompt injection"}],
                "sha256": "fake-sha",
                "truncated": False,
            },
        }

        task_data, _ = create_scoped_task(self.settings, malicious_body)
        snap = task_data["metadata"]["discussion_snapshot"]

        # Snapshot must reflect real server history, not the spoofed body
        self.assertEqual(snap["session_id"], sid)
        texts = [m["text"] for m in snap["messages"]]
        self.assertIn("Real server-stored turn", texts)
        self.assertNotIn("Spoofed prompt injection", texts)

    def test_excludes_system_and_tool_messages_and_redacts_text(self) -> None:
        """System, tool, and approval messages must be excluded; user and assistant text
        must be sanitized using the existing redaction helper.
        """
        wt = self._create_worktree("feat/filter-roles")
        sid = wt["chat_session_id"]

        secret_key = "sk-live-0123456789abcdef0123456789abcdef"
        self.store.append(sid, "system", "Internal system instructions")
        self.store.append(sid, "user", f"API token is {secret_key}")
        self.store.append(sid, "tool", "Tool execution output")
        self.store.append(sid, "assistant", "Token received safely.")

        task_data, _ = create_scoped_task(self.settings, {
            "repo_id": self.repo["id"],
            "worktree_id": wt["id"],
            "goal": "Check role filter and redaction",
        })
        snap = task_data["metadata"]["discussion_snapshot"]
        roles = [m["role"] for m in snap["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

        # Secret key must be redacted
        user_msg = next(m for m in snap["messages"] if m["role"] == "user")
        self.assertNotIn(secret_key, user_msg["text"])
        self.assertIn("[REDACTED", user_msg["text"])

    def test_transcript_read_error_fails_loudly_without_silent_discard(self) -> None:
        """If transcript store encounters an operational/read failure, create_scoped_task
        must raise a clear error and NOT silently discard the discussion.
        """
        wt = self._create_worktree("feat/read-error")
        sid = wt["chat_session_id"]
        self.store.append(sid, "user", "Critical specification that must not be lost")

        with patch.object(self.store, "recent_messages", side_effect=sqlite3.OperationalError("disk I/O error")):
            with patch("supervisor_service.get_transcript_store", return_value=self.store):
                with self.assertRaises(SupervisorServiceError) as cm:
                    create_scoped_task(self.settings, {
                        "repo_id": self.repo["id"],
                        "worktree_id": wt["id"],
                        "goal": "Should fail rather than lose context",
                    })
                self.assertEqual(cm.exception.status_code, 500)
                self.assertEqual(cm.exception.code, "transcript_read_error")

    def test_snapshot_filters_internal_assistant_kinds_before_latest_n(self) -> None:
        """Non-discussion kinds (tool_approval, tool_result, status, approval_prompt, internal)
        must be filtered BEFORE latest-N selection/counting. Legitimate user/assistant turns
        with kind=None, 'chat', or 'task_suggestion' must be retained even if user text mentions tools.
        """
        wt = self._create_worktree("feat/kind-filtering")
        sid = wt["chat_session_id"]

        # 4 legitimate discussion turns (legacy kind=None, 'chat', 'task_suggestion')
        self.store.append(sid, "user", "Can we run the tool to inspect test coverage?", kind="chat")
        self.store.append(sid, "assistant", "Yes, we will run the coverage tool.", kind="chat")
        self.store.append(sid, "user", "Please verify edge cases as well.")  # kind=None
        self.store.append(sid, "assistant", "I suggest adding boundary test cases.", kind="task_suggestion")

        # Now append >8 non-discussion assistant records (e.g. 10 internal/status/approval records)
        internal_kinds = [
            ("tool_approval", "Requesting permission to run pytest"),
            ("tool_result", "Test runner output: 42 passed"),
            ("status", "Task running in background"),
            ("approval_prompt", "Do you approve tool execution?"),
            ("internal", "Telemetry checkpoint reached"),
            ("tool_approval", "Requesting permission to run git status"),
            ("tool_result", "working tree clean"),
            ("status", "Task completed"),
            ("internal", "Worker idle"),
            ("approval_prompt", "Approval session closed"),
        ]
        for kind, content in internal_kinds:
            self.store.append(sid, "assistant", content, kind=kind)

        # Total session messages: 4 discussion + 10 internal = 14 messages
        total_all = self.store.count_messages(sid)
        self.assertEqual(total_all, 14)

        # Discussion snapshot with max_turns=8
        snap = capture_discussion_snapshot(self.settings, sid, max_turns=8)

        # Must capture all 4 discussion turns, excluding all 10 internal turns BEFORE latest-N
        self.assertEqual(len(snap["messages"]), 4)
        self.assertFalse(snap["truncated"])

        expected_texts = [
            "Can we run the tool to inspect test coverage?",
            "Yes, we will run the coverage tool.",
            "Please verify edge cases as well.",
            "I suggest adding boundary test cases.",
        ]
        actual_texts = [m["text"] for m in snap["messages"]]
        self.assertEqual(actual_texts, expected_texts)

        # User message mentioning 'tool' must be preserved
        self.assertIn("tool", actual_texts[0])

        # Verify digest contract matches exact messages
        expected_bytes = json.dumps(
            snap["messages"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(snap["sha256"], hashlib.sha256(expected_bytes).hexdigest())

        # If we now add 5 more legitimate discussion turns (total 9 > max_turns 8):
        for i in range(5):
            self.store.append(sid, "user" if i % 2 == 0 else "assistant", f"Extra discussion turn {i}", kind="chat")

        snap2 = capture_discussion_snapshot(self.settings, sid, max_turns=8)
        self.assertEqual(len(snap2["messages"]), 8)
        self.assertTrue(snap2["truncated"])
        # Oldest discussion turn was dropped to keep the latest 8 eligible turns
        self.assertEqual(snap2["messages"][0]["text"], "Yes, we will run the coverage tool.")


class SupervisorDiscussionSnapshotHttpTests(unittest.TestCase):
    """End-to-end HTTP integration test for discussion snapshot via WebConsoleServer."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "http-repo"
        _init_git_repo(self.repo_dir)

        self.harness = _HttpHarness(self.settings)

    def tearDown(self) -> None:
        self.harness.close()
        self.temp_dir.cleanup()

    def test_http_create_task_includes_discussion_snapshot(self) -> None:
        # Register repository via HTTP
        c_repo, repo = self.harness.request_json("POST", "/api/supervisor/repositories", {"path": str(self.repo_dir)})
        self.assertEqual(c_repo, 201)

        # Create managed worktree via HTTP
        c_wt, wt = self.harness.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/http-snapshot"},
        )
        self.assertEqual(c_wt, 201)

        # Seed chat messages into transcript store for this worktree session
        store = get_transcript_store(self.settings)
        store.append(wt["chat_session_id"], "user", "HTTP-level discussion topic")
        store.append(wt["chat_session_id"], "assistant", "HTTP-level plan confirmed")

        # Create task via HTTP
        c_task, task = self.harness.request_json(
            "POST",
            "/api/supervisor/tasks",
            {
                "repo_id": repo["id"],
                "worktree_id": wt["id"],
                "goal": "Verify HTTP task creation discussion snapshot",
            },
        )
        self.assertEqual(c_task, 201, task)

        # Inspect snapshot in task metadata
        meta = task.get("metadata", {})
        self.assertIn("discussion_snapshot", meta)
        snap = meta["discussion_snapshot"]
        self.assertEqual(snap["session_id"], wt["chat_session_id"])
        self.assertEqual(len(snap["messages"]), 2)
        self.assertEqual(snap["messages"][0]["text"], "HTTP-level discussion topic")
        self.assertEqual(snap["messages"][1]["text"], "HTTP-level plan confirmed")

        # GET /api/supervisor/tasks/:id also returns the snapshot
        c_get, task_detail = self.harness.request_json("GET", f"/api/supervisor/tasks/{task['id']}")
        self.assertEqual(c_get, 200)
        self.assertEqual(task_detail["metadata"]["discussion_snapshot"]["sha256"], snap["sha256"])

    def test_http_repositories_read_poll_preserves_archived_session(self) -> None:
        """Routine Code polling (GET /api/supervisor/repositories) must NOT reactivate
        archived sessions or touch their updated_at/title. A subsequent actual transcript
        append still reactivates and updates normally.
        """
        # Register repository via HTTP
        c_repo, repo = self.harness.request_json("POST", "/api/supervisor/repositories", {"path": str(self.repo_dir)})
        self.assertEqual(c_repo, 201)

        # Create managed worktree via HTTP
        c_wt, wt = self.harness.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/archive-poll-preservation"},
        )
        self.assertEqual(c_wt, 201)
        chat_sid = wt["chat_session_id"]
        store = get_transcript_store(self.settings)

        # Seed initial discussion turn to give it a defined state and title
        store.append(chat_sid, "user", "Discussion before archiving", kind="chat")
        store.append(chat_sid, "assistant", "Acknowledged before archiving", kind="chat")

        sess_before = store.get_session(chat_sid)
        self.assertIsNotNone(sess_before)
        self.assertEqual(sess_before["archived"], 0)

        # Archive the session
        self.assertTrue(store.archive_session(chat_sid))

        archived_sess = store.get_session(chat_sid)
        self.assertIsNotNone(archived_sess)
        self.assertEqual(archived_sess["archived"], 1)
        archived_updated_at = archived_sess["updated_at"]
        archived_title = archived_sess["title"]

        # Small sleep so any erroneous updated_at touch would advance timestamp
        time.sleep(0.05)

        # Repeated GET /api/supervisor/repositories polls (simulating routine UI read polls)
        for _ in range(3):
            c_poll, poll_repos = self.harness.request_json("GET", "/api/supervisor/repositories")
            self.assertEqual(c_poll, 200)

        # Session MUST remain archived with unchanged updated_at and title
        sess_after_polls = store.get_session(chat_sid)
        self.assertIsNotNone(sess_after_polls)
        self.assertEqual(sess_after_polls["archived"], 1, "read polling must not reactivate archived session")
        self.assertEqual(sess_after_polls["updated_at"], archived_updated_at, "read polling must not touch updated_at")
        self.assertEqual(sess_after_polls["title"], archived_title, "read polling must not touch title")

        # Now perform an actual new transcript append
        time.sleep(0.05)
        store.append(chat_sid, "user", "Actual user message resuming conversation", kind="chat")

        # Session MUST now be reactivated with an updated timestamp
        sess_reactivated = store.get_session(chat_sid)
        self.assertIsNotNone(sess_reactivated)
        self.assertEqual(sess_reactivated["archived"], 0, "actual append must reactivate archived session")
        self.assertGreater(sess_reactivated["updated_at"], archived_updated_at, "actual append must advance updated_at")


if __name__ == "__main__":
    unittest.main()
