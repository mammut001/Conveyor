"""tests/test_supervisor_worker_ownership.py — Targeted tests for supervisor worker
flow ownership, cancellation precision, evidence isolation, and done callback safety.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
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

from conveyor.agents import agent_supervisor as ags
from config import Settings
from conveyor.agents.supervisor_service import (
    SupervisorWorker,
    SupervisorWorkerManager,
    cancel_scoped_task,
    get_scoped_task,
)


def _make_test_settings(root: Path) -> Settings:
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
        conveyor_web_token="test-token-12345678901234567890",
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


def _init_repo(repo_dir: Path) -> str:
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


class SupervisorWorkerOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.settings = _make_test_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)

        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()

        self.worker = SupervisorWorkerManager(self.loop)
        self.worker.ensure_started(self.settings)

    def tearDown(self) -> None:
        self.worker.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=3.0)
        self.loop.close()
        self.temp_dir.cleanup()

    def _create_handle_on_loop(
        self,
        execution_id: str,
        proc: Any,
        task_id: str,
        worktree_path: str,
        role: str,
    ) -> ags._LiveHandle:
        async def _make() -> ags._LiveHandle:
            return ags._LiveHandle(execution_id, proc, task_id, worktree_path, role)

        fut = asyncio.run_coroutine_threadsafe(_make(), self.loop)
        return fut.result(timeout=2.0)

    def test_actual_bound_flow_cancel_does_not_terminate_unrelated_adhoc_child(self) -> None:
        """Verified exact task cancel must terminate its own bound execution while leaving
        an actual unrelated adhoc helper child process (empty task_id, matching owner/time) running.
        Probe process is cleaned up after verification.
        """
        wt_dir = self.root / "wt-cancel"
        subprocess.run(["git", "clone", str(self.repo_dir), str(wt_dir)], check=True, capture_output=True)
        task = ags.create_task(
            self.settings,
            goal="Test bound cancel precision",
            repo_path=str(self.repo_dir),
            worktree_path=str(wt_dir),
        )
        task_id = str(task["id"])
        ags.transition(self.settings, task_id, "queued")
        ags.transition(self.settings, task_id, "dispatching")
        ags.transition(self.settings, task_id, "implementing")
        ags.acquire_worktree_lock(self.settings, str(wt_dir), task_id)

        # 1. Spawn a REAL probe process representing an unrelated adhoc helper command (task_id="").
        adhoc_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=os.setsid,
        )
        # 2. Spawn a REAL process representing the exact task-bound execution.
        bound_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=os.setsid,
        )

        owner_id = ags.runtime_owner_id()
        owner_pid = os.getpid()
        owner_ident = ags._process_identity(owner_pid) or ""
        now = ags._utc_now()

        adhoc_eid = f"ex-adhoc-{os.urandom(6).hex()}"
        bound_eid = f"ex-bound-{os.urandom(6).hex()}"

        try:
            # Register both in SQLite supervisor_executions with real OS PID/PGID/identity.
            conn = ags._connect(self.settings)
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Adhoc execution: task_id="" (unbound), matching owner, start time >= task created_at.
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, owner_pid, owner_process_identity,
                        status, started_at, finished_at, evidence_path, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        adhoc_eid,
                        "",
                        "adhoc_helper",
                        "python",
                        "",
                        "sha_adhoc",
                        adhoc_proc.pid,
                        os.getpgid(adhoc_proc.pid),
                        ags._process_identity(adhoc_proc.pid) or "",
                        owner_id,
                        owner_pid,
                        owner_ident,
                        "running",
                        now,
                        None,
                        "",
                        json.dumps({}),
                    ),
                )
                # Task-bound execution: task_id=task_id.
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, owner_pid, owner_process_identity,
                        status, started_at, finished_at, evidence_path, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        bound_eid,
                        task_id,
                        "implementer",
                        "python",
                        "",
                        "sha_bound",
                        bound_proc.pid,
                        os.getpgid(bound_proc.pid),
                        ags._process_identity(bound_proc.pid) or "",
                        owner_id,
                        owner_pid,
                        owner_ident,
                        "running",
                        now,
                        None,
                        "",
                        json.dumps({}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            # Simulate worker owning strictly this single active flow.
            # Under the prior buggy code, active_snapshot == [task_id] would guess and kill adhoc_proc!
            async def _dummy_flow() -> None:
                await asyncio.sleep(60)

            flow_task = self.worker.loop.create_task(_dummy_flow())
            self.worker._active_flows[task_id] = flow_task

            # Cancel the task through worker.cancel_flow
            self.worker.cancel_flow(self.settings, task_id, reason="operator cancelled flow")

            # Verify: Bound process must be cancelled and terminated.
            wait_bound = time.monotonic() + 5.0
            bound_terminated = False
            while time.monotonic() < wait_bound:
                if bound_proc.poll() is not None:
                    bound_terminated = True
                    break
                time.sleep(0.05)
            self.assertTrue(bound_terminated, "exact task bound process must be terminated upon cancel")

            bound_row = ags._execution_row(self.settings, bound_eid) or {}
            self.assertEqual(bound_row.get("status"), "cancelled", bound_row)

            # Verify: Adhoc process must NOT be terminated! It must remain running!
            self.assertIsNone(adhoc_proc.poll(), "unrelated adhoc child process must NOT be terminated by cancel_flow")
            adhoc_row = ags._execution_row(self.settings, adhoc_eid) or {}
            self.assertEqual(adhoc_row.get("status"), "running", "adhoc execution row must remain 'running', not cancelled")

        finally:
            # Clean up probe processes safely.
            for proc in (adhoc_proc, bound_proc):
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass

    def test_task_details_exclude_adhoc_unrelated_evidence(self) -> None:
        """Task details via get_scoped_task must exclude unrelated adhoc execution rows,
        preserving true bound rows and evidence paths without guessing by timestamp.
        """
        wt_dir = self.root / "wt-details"
        subprocess.run(["git", "clone", str(self.repo_dir), str(wt_dir)], check=True, capture_output=True)
        task = ags.create_task(
            self.settings,
            goal="Evidence scoping test",
            repo_path=str(self.repo_dir),
            worktree_path=str(wt_dir),
        )
        task_id = str(task["id"])

        evidence_dir = self.root / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        bound_evidence_file = evidence_dir / "bound_ev.json"
        bound_evidence_file.write_text(json.dumps({"detail": "valid-bound-evidence"}), encoding="utf-8")
        unrelated_evidence_file = evidence_dir / "unrelated_ev.json"
        unrelated_evidence_file.write_text(json.dumps({"detail": "adhoc-unrelated"}), encoding="utf-8")

        # Record a handoff pointing to bound_evidence_file
        ags.record_handoff(
            self.settings,
            task_id=task_id,
            from_role="implementer",
            to_role="reviewer",
            summary="Implementation done",
            evidence_path=str(bound_evidence_file),
        )

        owner_id = ags.runtime_owner_id()
        owner_pid = os.getpid()
        owner_ident = ags._process_identity(owner_pid) or ""
        now = ags._utc_now()

        # 1. Bound execution (task_id=task_id)
        eid_bound = f"ex-bnd-{os.urandom(6).hex()}"
        # 2. Legacy unbound execution matching evidence_path (task_id="", evidence_path=bound_evidence_file)
        eid_legacy_ev = f"ex-leg-{os.urandom(6).hex()}"
        # 3. Adhoc unrelated execution (task_id="", evidence_path=unrelated_evidence_file, matching owner/time)
        eid_unrelated = f"ex-unr-{os.urandom(6).hex()}"

        conn = ags._connect(self.settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for eid, tid, ep in (
                (eid_bound, task_id, ""),
                (eid_legacy_ev, "", str(bound_evidence_file)),
                (eid_unrelated, "", str(unrelated_evidence_file)),
            ):
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, owner_pid, owner_process_identity,
                        status, started_at, finished_at, evidence_path, metadata_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        eid,
                        tid,
                        "worker",
                        "python",
                        "",
                        "sha",
                        owner_pid,
                        os.getpgrp(),
                        owner_ident,
                        owner_id,
                        owner_pid,
                        owner_ident,
                        "completed",
                        now,
                        now,
                        ep or "",
                        json.dumps({}),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        scoped = get_scoped_task(self.settings, task_id)
        executions = scoped.get("executions", [])
        retrieved_ids = {str(e.get("id")) for e in executions}

        # True bound execution and matching evidence execution MUST be present.
        self.assertIn(eid_bound, retrieved_ids, "true task-bound execution must be included")
        self.assertIn(eid_legacy_ev, retrieved_ids, "execution with matching handoff evidence path must be included")

        # Unrelated adhoc execution MUST be excluded.
        self.assertNotIn(eid_unrelated, retrieved_ids, "adhoc unrelated execution must be strictly excluded from task details")

        # Legacy unbound execution must not have its task_id misrepresented as the bound task_id.
        legacy_item = next(e for e in executions if e["id"] == eid_legacy_ev)
        self.assertEqual(legacy_item.get("task_id", ""), "", "legacy unbound row must not be rewritten as bound task_id")

    def test_unit_fault_done_callback_consumes_exception_and_fails_nonterminal_task(self) -> None:
        """Unit fault test: Inject unexpected exception into flow future; done callback must
        consume the exception without leaking unretrieved errors, redact error logging,
        and terminalize nonterminal task strictly to 'failed'.
        """
        wt_dir = self.root / "wt-fault"
        subprocess.run(["git", "clone", str(self.repo_dir), str(wt_dir)], check=True, capture_output=True)
        task = ags.create_task(
            self.settings,
            goal="Fault injection test",
            repo_path=str(self.repo_dir),
            worktree_path=str(wt_dir),
        )
        task_id = str(task["id"])
        ags.transition(self.settings, task_id, "queued")
        ags.transition(self.settings, task_id, "dispatching")
        ags.transition(self.settings, task_id, "implementing")
        ags.acquire_worktree_lock(self.settings, str(wt_dir), task_id)

        # Create a coroutine that raises an unexpected exception containing sensitive token.
        secret_token = "secret-token-leak-check-42"
        async def _failing_flow() -> None:
            raise RuntimeError(f"worker failure with {secret_token}")

        fault_task = self.worker.loop.create_task(_failing_flow())
        self.worker._active_flows[task_id] = fault_task

        # Register callback using worker's _on_flow_done
        fault_task.add_done_callback(
            lambda fut, tid=task_id, s=self.settings: self.worker._on_flow_done(s, tid, fut)
        )

        # Wait for task completion on worker's event loop
        wait_deadline = time.monotonic() + 5.0
        while time.monotonic() < wait_deadline:
            if fault_task.done():
                break
            time.sleep(0.05)
        self.assertTrue(fault_task.done(), "faulty task must complete")
        time.sleep(0.1)  # allow done callback to execute

        # 1. Active flow entry must be cleared.
        self.assertNotIn(task_id, self.worker._active_flows)

        # 2. Task state in SQLite must be strictly 'failed' (no permissive any-terminal asserts).
        cur_task = ags.get_task(self.settings, task_id) or {}
        self.assertEqual(cur_task.get("state"), "failed", f"task must transition strictly to failed, got: {cur_task.get('state')}")
        self.assertEqual(cur_task.get("stage"), "unexpected_error")

        # 3. Worktree lock must be safely released because writer liveness was 'none'.
        self.assertTrue(ags.has_active_task_for_worktree(self.settings, str(wt_dir)) is False)

    def test_done_callback_preserves_terminal_cancelled_state(self) -> None:
        """When a task is already in terminal state 'cancelled', the done callback must
        NOT mutate or upgrade it to 'failed' even if the flow coroutine raised an exception.
        """
        wt_dir = self.root / "wt-cancel-preserve"
        subprocess.run(["git", "clone", str(self.repo_dir), str(wt_dir)], check=True, capture_output=True)
        task = ags.create_task(
            self.settings,
            goal="Cancel preserve test",
            repo_path=str(self.repo_dir),
            worktree_path=str(wt_dir),
        )
        task_id = str(task["id"])
        ags.transition(self.settings, task_id, "queued")
        ags.transition(self.settings, task_id, "cancelled", stage="cancelled", error="user cancel")

        async def _late_failing_coro() -> None:
            raise RuntimeError("crash after cancel")

        flow_task = self.worker.loop.create_task(_late_failing_coro())
        self.worker._active_flows[task_id] = flow_task
        flow_task.add_done_callback(
            lambda fut, tid=task_id, s=self.settings: self.worker._on_flow_done(s, tid, fut)
        )

        wait_deadline = time.monotonic() + 5.0
        while time.monotonic() < wait_deadline:
            if flow_task.done():
                break
            time.sleep(0.05)
        self.assertTrue(flow_task.done())
        time.sleep(0.1)

        cur_task = ags.get_task(self.settings, task_id) or {}
        self.assertEqual(cur_task.get("state"), "cancelled", "terminal cancelled state must NOT be changed to failed")

    def test_uncertain_writer_fence_retained_on_done(self) -> None:
        """Worktree lock must ONLY be released when ags.lock_release_safe proves writer dead.
        If writer fence has uncertain/active liveness, the lock row must be retained.
        """
        wt_dir = self.root / "wt-uncertain-fence"
        subprocess.run(["git", "clone", str(self.repo_dir), str(wt_dir)], check=True, capture_output=True)
        task = ags.create_task(
            self.settings,
            goal="Fence retention test",
            repo_path=str(self.repo_dir),
            worktree_path=str(wt_dir),
        )
        task_id = str(task["id"])
        ags.transition(self.settings, task_id, "queued")
        ags.transition(self.settings, task_id, "dispatching")
        ags.transition(self.settings, task_id, "implementing")
        canonical_wp = ags._canonical_path(str(wt_dir))
        ags.acquire_worktree_lock(self.settings, canonical_wp, task_id)

        # Set an active writer fence on the lock row pointing to a live process (current test runner pid).
        live_pid = os.getpid()
        live_pgid = os.getpgrp()
        live_ident = ags._process_identity(live_pid) or ""

        conn = ags._connect(self.settings)
        try:
            conn.execute(
                "UPDATE supervisor_locks SET writer_pid = ?, writer_pgid = ?, writer_identity = ?"
                " WHERE task_id = ?",
                (live_pid, live_pgid, live_ident, task_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Verify ags.lock_release_safe returns False for this active/uncertain writer fence
        self.assertFalse(ags.lock_release_safe(self.settings, canonical_wp, task_id))

        # Transition task to failed
        ags.transition(self.settings, task_id, "failed", stage="failure_check")

        # Now trigger done callback
        dummy_fut: asyncio.Future[Any] = self.worker.loop.create_future()
        self.worker.loop.call_soon_threadsafe(dummy_fut.set_result, None)
        self.worker._active_flows[task_id] = dummy_fut

        self.worker._on_flow_done(self.settings, task_id, dummy_fut)

        # The worktree lock MUST NOT be cleared because writer is not dead!
        conn_ro = ags._connect_ro(self.settings)
        try:
            row = conn_ro.execute(
                "SELECT task_id, writer_pid, writer_identity FROM supervisor_locks WHERE worktree_path = ?",
                (canonical_wp,),
            ).fetchone()
            self.assertIsNotNone(row, "uncertain/active writer fence must be retained in supervisor_locks")
            self.assertEqual(row["task_id"], task_id)
        finally:
            conn_ro.close()

    def test_stale_callback_cannot_drop_new_flow_same_id(self) -> None:
        """When a task flow is retried or replaced under the same task_id, a stale callback
        from the previous future must NOT pop the new flow from _active_flows.
        """
        task_id = "task-retry-same-id-1234"
        old_fut: asyncio.Future[Any] = self.worker.loop.create_future()
        new_fut: asyncio.Future[Any] = self.worker.loop.create_future()
        self.worker.loop.call_soon_threadsafe(old_fut.set_result, None)
        self.worker.loop.call_soon_threadsafe(new_fut.set_result, None)
        time.sleep(0.05)

        # Map currently points to new_fut
        self.worker._active_flows[task_id] = new_fut

        # Old future finishes and its callback runs with old_fut
        self.worker._on_flow_done(self.settings, task_id, old_fut)

        # new_fut must STILL be in _active_flows
        self.assertIn(task_id, self.worker._active_flows)
        self.assertIs(self.worker._active_flows[task_id], new_fut, "stale callback must not pop new flow with same task_id")

        # When new_fut's own callback runs, entry is popped cleanly
        self.worker._on_flow_done(self.settings, task_id, new_fut)
        self.assertNotIn(task_id, self.worker._active_flows)

    def test_done_callback_handles_db_error_honestly_without_phantom_completed(self) -> None:
        """If database queries raise an operational error during done callback, the error
        must be caught, logged honestly, and must NOT fabricate a phantom completed state
        or improperly clear locks.
        """
        task_id = "task-dberr-test-5678"
        fault_fut: asyncio.Future[Any] = self.worker.loop.create_future()
        self.worker.loop.call_soon_threadsafe(
            fault_fut.set_exception, RuntimeError("coro failed during DB outage")
        )
        time.sleep(0.05)

        with patch("conveyor.agents.agent_supervisor.get_task", side_effect=sqlite3.OperationalError("database disk image is malformed")):
            # Must not crash worker loop or raise unhandled exception
            try:
                self.worker._on_flow_done(self.settings, task_id, fault_fut)
            except Exception as exc:
                self.fail(f"_on_flow_done should handle DB error gracefully, raised: {exc}")


if __name__ == "__main__":
    unittest.main()
