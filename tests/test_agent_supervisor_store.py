"""Supervisor store/lifecycle invariants: locks, queue, acceptance by content."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from conveyor.core.config import Settings
from conveyor.agents import agent_supervisor as ags
from conveyor.agents.agent_supervisor import (
    SupervisorLockError, SupervisorStateError,
    acquire_worktree_lock, cancel_task, claim_task, create_task,
    dispatch_task, effective_acceptance, get_task, has_active_task_for_worktree,
    latest_acceptance, list_executions, list_handoffs, load_roles, queue_task,
    record_acceptance, release_worktree_lock, start_repair, target_identity,
    transition, validate_worktree,
)


def make_settings(tmp: Path) -> Settings:
    return Settings(
        telegram_bot_token="", telegram_allowed_user_id=0,
        codex_workspace_root=tmp, codex_bin="codex", codex_task_root=tmp,
        codex_model=None, codex_timeout_seconds=60, telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(), codex_memory_root=tmp / "memory",
        user_timezone="UTC", agent_supervisor_enabled=True,
    )


def git(*argv: str, cwd: Path) -> str:
    r = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {r.stderr[:300]}")
    return (r.stdout or "").strip()


def make_repo(tmp: Path) -> tuple[Path, Path, str]:
    repo = tmp / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@t.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "a.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    wt = tmp / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), head],
                   check=True, capture_output=True)
    return repo, wt, head


class FakeExecutor:
    name = "fake"

    def __init__(self) -> None:
        self.review_count = 0
        self.implement_count = 0
        self.role_calls: list[str] = []

    def available(self):
        return True, "ok"

    async def run(self, *, role, prompt, worktree_path, timeout_seconds):
        self.role_calls.append(role.name)
        if role.name in ("analyst", "coordinator"):
            return ags.ExecutionResult(success=True, output="plan ok",
                                       started_at=ags._utc_now(), finished_at=ags._utc_now())
        if role.name == "reviewer":
            self.review_count += 1
            if self.review_count > 1:
                envelope = {
                    "passed": True,
                    "target_sha": ags._git_head(worktree_path),
                    "target_fingerprint": target_identity(worktree_path)["fingerprint"],
                    "issues": [],
                    "checks": [{"name": "tests", "command": "true", "passed": True,
                                "output": "ok"}],
                    "summary": "acceptable",
                }
            else:
                envelope = {
                    "passed": False,
                    "target_sha": ags._git_head(worktree_path),
                    "target_fingerprint": target_identity(worktree_path)["fingerprint"],
                    "issues": ["not deterministic"],
                    "checks": [{"name": "tests", "command": "true", "passed": False,
                                "output": "fail"}],
                    "summary": "flawed",
                }
            return ags.ExecutionResult(success=True, output=json.dumps(envelope),
                                       started_at=ags._utc_now(), finished_at=ags._utc_now())
        self.implement_count += 1
        (Path(worktree_path) / "impl.txt").write_text(f"r{self.implement_count}")
        subprocess.run(["git", "-C", worktree_path, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", worktree_path, "commit", "-qm", f"r{self.implement_count}"],
                       check=True, capture_output=True)
        return ags.ExecutionResult(success=True, output="done",
                                   started_at=ags._utc_now(), finished_at=ags._utc_now())

    async def cancel(self, execution_id):
        return False


class SupervisorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = make_settings(self.tmp)
        self.repo, self.wt, self.head = make_repo(self.tmp)
        self.fake = FakeExecutor()
        real_get_executor = ags.get_executor
        patcher = patch.object(
            ags,
            "get_executor",
            lambda s, name: self.fake if (name or "").strip().lower() == "fake" else real_get_executor(s, name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_worktree_validation_rejects_untied_paths(self) -> None:
        ok, _ = validate_worktree(self.settings, str(self.repo), str(self.wt))
        self.assertTrue(ok)
        # a git repo that is not the task repo's worktree must be rejected
        other = self.tmp / "other"
        other.mkdir()
        git("init", "-q", cwd=other)
        git("config", "user.email", "t@t.com", cwd=other)
        git("config", "user.name", "t", cwd=other)
        (other / "x").write_text("x")
        subprocess.run(["git", "add", "."], cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "x"], cwd=other, check=True, capture_output=True)
        ok, reason = validate_worktree(self.settings, str(self.repo), str(other))
        self.assertFalse(ok)
        self.assertIn("不属于所选仓库", reason)
        # a plain dir in the managed root (not git) must be rejected
        plain = self.tmp / "plain"
        plain.mkdir()
        ok, _ = validate_worktree(self.settings, str(self.repo), str(plain))
        self.assertFalse(ok)

    def test_target_identity_covers_untracked_content(self) -> None:
        base = target_identity(str(self.wt))
        (self.wt / "untracked.txt").write_text("junk")
        changed = target_identity(str(self.wt))
        self.assertNotEqual(base["fingerprint"], changed["fingerprint"])
        self.assertEqual(base["sha"], changed["sha"])

    def test_lock_takeover_only_when_provable_or_terminal(self) -> None:
        t = create_task(self.settings, goal="g", repo_path=str(self.repo), base_sha=self.head)
        acquire_worktree_lock(self.settings, str(self.wt), t["id"])
        with self.assertRaises(SupervisorLockError):
            acquire_worktree_lock(self.settings, str(self.wt), "sup-other")
        # lock ended: the owning task is terminal => takeover allowed even within TTL
        transition(self.settings, t["id"], "failed", stage="x")
        acquire_worktree_lock(self.settings, str(self.wt), "sup-other")

    def test_lock_not_free_until_writer_gone(self) -> None:
        t = create_task(self.settings, goal="g", repo_path=str(self.repo), base_sha=self.head)
        acquire_worktree_lock(self.settings, str(self.wt), t["id"])
        probe = subprocess.Popen(["/bin/sh", "-c", "sleep 300"], cwd=str(self.wt),
                                 start_new_session=True)

        def _cleanup_probe() -> None:
            if probe.poll() is None:
                try:
                    os.killpg(probe.pid, 9)
                except OSError:
                    pass
                probe.wait(timeout=3)

        self.addCleanup(_cleanup_probe)
        writer_ident = ags._process_identity(probe.pid)
        self.assertTrue(writer_ident)
        ags.record_lock_writer_pgid(self.settings, t["id"], probe.pid)
        # expire the lock, but a live writer means no takeover may occur
        conn = ags._connect(self.settings)
        with conn:
            conn.execute("UPDATE supervisor_locks SET expires_at = ? WHERE task_id = ?",
                         (ags._utc_now(), t["id"]))
        conn.close()
        t0 = time.monotonic()
        with self.assertRaises(SupervisorLockError):
            acquire_worktree_lock(self.settings, str(self.wt), "sup-other")
        # Must fail fast via writer liveness check, never a 10s SQLite busy timeout.
        self.assertLess(time.monotonic() - t0, 1.0)
        # writer provably gone => takeover
        os.killpg(probe.pid, 9)
        probe.wait(timeout=5)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if ags._verify_process_identity(probe.pid, writer_ident) is False and ags._pgid_alive(probe.pid) is False:
                break
            time.sleep(0.02)
        self.assertIs(ags._verify_process_identity(probe.pid, writer_ident), False)
        self.assertIs(ags._pgid_alive(probe.pid), False)
        acquire_worktree_lock(self.settings, str(self.wt), "sup-other")

    def test_queue_validation_rejects_unknown_executor_before_dispatch(self) -> None:
        fake_settings = make_settings(self.tmp)
        t = create_task(fake_settings, goal="g", repo_path=str(self.repo), base_sha=self.head)
        with self.assertRaises(SupervisorStateError):
            queue_task(fake_settings, t["id"], worktree_path=str(self.wt),
                       roles={"implementer": "definitely-not-real"})

    def test_completed_requires_passing_content_bound_acceptance(self) -> None:
        t = create_task(self.settings, goal="g", repo_path=str(self.repo),
                        worktree_path=str(self.wt), base_sha=self.head)
        transition(self.settings, t["id"], "implementing", stage="x")
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, t["id"], "completed")
        with self.assertRaises(SupervisorStateError):
            record_acceptance(self.settings, t["id"], sha=self.head, passed=True,
                              issues=[], evidence="review", fingerprint="")
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, t["id"], "completed")
        identity = target_identity(str(self.wt))
        record_acceptance(self.settings, t["id"], sha=identity["sha"], passed=True,
                          issues=[], evidence="review", fingerprint=identity["fingerprint"])
        transition(self.settings, t["id"], "completed")

    def test_acceptance_invalidated_when_untracked_content_changes(self) -> None:
        t = create_task(self.settings, goal="g", repo_path=str(self.repo),
                        worktree_path=str(self.wt), base_sha=self.head)
        transition(self.settings, t["id"], "implementing", stage="x")
        identity = target_identity(str(self.wt))
        acc = record_acceptance(self.settings, t["id"], sha=identity["sha"], passed=True,
                                issues=[], evidence="review", fingerprint=identity["fingerprint"])
        self.assertIsNotNone(effective_acceptance(self.settings, t["id"]))
        # later untracked modification invalidates without any sha change
        (self.wt / "scratch-untracked.txt").write_text("later junk")
        self.assertIsNone(effective_acceptance(self.settings, t["id"]))
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, t["id"], "completed")
        self.assertTrue(acc["passed"])

    def test_evidence_paths_recorded(self) -> None:
        settings = self.settings
        t = create_task(settings, goal="g", repo_path=str(self.repo), base_sha=self.head)
        qt = queue_task(settings, t["id"], worktree_path=str(self.wt),
                        roles={r: "fake" for r in ags.ROLES})
        self.assertEqual(qt["state"], "queued")
        claimed = claim_task(settings)
        self.assertEqual(claimed["id"], t["id"])
        self.assertEqual(claimed["state"], "dispatching")

    def test_full_flow_with_fake_executor(self) -> None:
        flow_settings = dataclasses.replace(
            self.settings,
            agent_supervisor_validation_commands=(
                (
                    "verify-impl",
                    (
                        sys.executable,
                        "-c",
                        "import pathlib, sys; sys.exit(0 if pathlib.Path('impl.txt').exists() else 1)",
                    ),
                ),
            ),
        )
        t = create_task(flow_settings, goal="add impl.txt", repo_path=str(self.repo),
                        base_sha=self.head)
        queue_task(flow_settings, t["id"], worktree_path=str(self.wt),
                   roles={r: "fake" for r in ags.ROLES})
        claim = claim_task(flow_settings)
        self.assertEqual(claim["id"], t["id"])
        import asyncio
        final = asyncio.run(ags.run_supervisor_flow(flow_settings, t["id"]))
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["repair_round"], 1)
        self.assertEqual(
            self.fake.role_calls,
            ["coordinator", "analyst", "implementer", "reviewer", "implementer", "reviewer"],
        )
        handoffs = list_handoffs(flow_settings, t["id"])
        role_handoffs = [(h["from_role"], h["to_role"]) for h in handoffs if h["from_role"] != "system"]
        self.assertEqual(
            role_handoffs,
            [
                ("coordinator", "analyst"),
                ("analyst", "implementer"),
                ("implementer", "reviewer"),
                ("implementer", "reviewer"),
            ],
        )
        acc = effective_acceptance(flow_settings, t["id"])
        self.assertIsNotNone(acc)
        self.assertTrue(acc["passed"])
        self.assertTrue(acc["provenance_verified"])
        self.assertEqual(len(acc["executed_checks"]), 1)
        self.assertEqual(acc["executed_checks"][0]["name"], "verify-impl")
        # FakeExecutor is in-memory; the 2 operator validation runs are real subprocess rows.
        rows = list_executions(flow_settings, task_id=t["id"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["role"] == "validator" and r["status"] == "finished" for r in rows))
        self.assertTrue(all(r["evidence_path"] and Path(r["evidence_path"]).is_file() for r in rows))

    def test_duplicate_dispatch_for_worktree(self) -> None:
        a = create_task(self.settings, goal="a", repo_path=str(self.repo), base_sha=self.head)
        b = create_task(self.settings, goal="b", repo_path=str(self.repo), base_sha=self.head)
        dispatch_task(self.settings, a["id"], worktree_path=str(self.wt))
        with self.assertRaises(SupervisorLockError):
            dispatch_task(self.settings, b["id"], worktree_path=str(self.wt))
        self.assertTrue(has_active_task_for_worktree(
            self.settings, str(self.wt), exclude_task_id=b["id"]))
        cancel_task(self.settings, a["id"])
        self.assertFalse(has_active_task_for_worktree(self.settings, str(self.wt)))


if __name__ == "__main__":
    unittest.main()
