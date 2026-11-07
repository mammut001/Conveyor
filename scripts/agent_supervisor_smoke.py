#!/usr/bin/env python3
"""Offline smoke test for Agent Supervisor runtime support.

Validates state machine invariants, database store consistency, lock lifecycles,
SHA and content binding invalidations, repair caps, and deployment store tracking
using real disposable git repositories and worktrees without invoking actual models.

Note:
- Offline smoke verifies runtime invariants and store mechanics only.
- Fake/mock executor executions are NOT counted as real model proof.
- Opt-in live model evaluation is isolated in scripts/agent_supervisor_live.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from config import Settings
import agent_supervisor as ags
from agent_supervisor import (
    SupervisorLockError,
    SupervisorStateError,
    acquire_worktree_lock,
    cancel_task,
    create_deployment,
    create_task,
    create_worktree,
    dispatch_task,
    effective_acceptance,
    enabled,
    get_deployment,
    get_task,
    has_active_task_for_worktree,
    list_handoffs,
    record_acceptance,
    record_handoff,
    recover_on_startup,
    release_worktree_lock,
    start_repair,
    summary,
    target_identity,
    transition,
    update_deployment_stage,
)


def run_git(cwd: Path | str, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agent-supervisor-smoke-"))
    try:
        # 1. Setup disposable git repository
        repo_dir = tmp / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        run_git(repo_dir, "init")
        run_git(repo_dir, "config", "user.name", "Conveyor Smoke")
        run_git(repo_dir, "config", "user.email", "smoke@conveyor.local")
        (repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        run_git(repo_dir, "add", "README.md")
        run_git(repo_dir, "commit", "-m", "initial commit")
        initial_sha = run_git(repo_dir, "rev-parse", "HEAD")
        assert len(initial_sha) == 40, f"invalid initial sha: {initial_sha}"

        # 2. Setup isolated Settings and directory layout
        settings = Settings(
            telegram_bot_token="",
            telegram_allowed_user_id=0,
            codex_workspace_root=tmp / "workspace",
            codex_bin="codex",
            codex_task_root=tmp / "tasks",
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=tmp / "memory",
            user_timezone="UTC",
            agent_supervisor_enabled=True,
            agent_supervisor_max_repair_rounds=2,
        )
        settings.codex_memory_root.mkdir(parents=True, exist_ok=True)
        (settings.codex_memory_root / "state").mkdir(parents=True, exist_ok=True)

        assert enabled(settings) is True, "Agent supervisor must be enabled"

        # 3. Create task and supervised worktree
        t1 = create_task(
            settings,
            goal="Add profile settings view",
            repo_path=str(repo_dir),
            base_sha=initial_sha,
        )
        assert t1["state"] == "pending_dispatch"
        tid = t1["id"]

        wt1 = create_worktree(settings, str(repo_dir), tid)
        assert Path(wt1).is_dir()
        identity1 = target_identity(wt1)
        assert identity1["sha"] == initial_sha

        # 4. Dispatch task & verify worktree lock
        dt1 = dispatch_task(settings, tid, worktree_path=wt1, base_sha=initial_sha)
        assert dt1["state"] == "analyzing"
        assert dt1["worktree_path"] == wt1

        # Invariant: worktree lock is strictly held
        assert has_active_task_for_worktree(settings, wt1) is True
        try:
            acquire_worktree_lock(settings, wt1, "competing-task-id")
            raise AssertionError("FAIL: acquire_worktree_lock should fail on locked worktree")
        except SupervisorLockError:
            pass

        # Invariant: duplicate task dispatch to same worktree is rejected
        t_conflict = create_task(settings, goal="Conflicting task", repo_path=str(repo_dir))
        try:
            dispatch_task(settings, t_conflict["id"], worktree_path=wt1, base_sha=initial_sha)
            raise AssertionError("FAIL: dispatch_task should reject duplicate worktree assignment")
        except SupervisorLockError:
            pass

        # 5. Handoffs & state transitions
        record_handoff(
            settings,
            tid,
            from_role="coordinator",
            to_role="analyst",
            summary="Initial analysis plan",
            sha=initial_sha,
        )
        handoffs = list_handoffs(settings, tid)
        assert len(handoffs) >= 1

        transition(settings, tid, "implementing", stage="implementation")
        assert get_task(settings, tid)["state"] == "implementing"

        transition(settings, tid, "reviewing", stage="review")
        assert get_task(settings, tid)["state"] == "reviewing"

        # 6. Acceptance invariants: SHA targeting, fingerprinting, and invalidations
        # Invariant: empty or invalid length SHA is rejected
        try:
            record_acceptance(settings, tid, sha="", passed=True)
            raise AssertionError("FAIL: empty SHA accepted")
        except (SupervisorStateError, ValueError):
            pass

        try:
            record_acceptance(settings, tid, sha="abc123", passed=True)
            raise AssertionError("FAIL: malformed non-40-hex SHA accepted")
        except (SupervisorStateError, ValueError):
            pass

        # Invariant: mismatched SHA (not matching worktree HEAD) is rejected
        try:
            record_acceptance(settings, tid, sha="0" * 40, passed=True)
            raise AssertionError("FAIL: mismatched SHA accepted")
        except SupervisorStateError:
            pass

        # Invariant: passing acceptance cannot contain unresolved issues
        try:
            record_acceptance(settings, tid, sha=initial_sha, passed=True, issues=["critical bug"])
            raise AssertionError("FAIL: passing acceptance with unresolved issues accepted")
        except SupervisorStateError:
            pass

        # Valid failing acceptance recorded
        acc_fail = record_acceptance(
            settings,
            tid,
            sha=initial_sha,
            passed=False,
            issues=["edge case missing"],
            evidence="unit test failure log",
        )
        assert acc_fail["passed"] is False
        eff = effective_acceptance(settings, tid)
        assert eff is not None and eff["passed"] is False

        # Valid passing acceptance recorded
        acc_pass = record_acceptance(
            settings,
            tid,
            sha=initial_sha,
            passed=True,
            evidence="all operator checks passed",
        )
        assert acc_pass["passed"] is True
        eff_pass = effective_acceptance(settings, tid)
        assert eff_pass is not None and eff_pass["passed"] is True

        # Invariant: Content tampering (untracked file in worktree) invalidates acceptance
        tamper_file = Path(wt1) / "tamper_edit.txt"
        tamper_file.write_text("untracked tamper", encoding="utf-8")
        assert effective_acceptance(settings, tid) is None, (
            "acceptance should be invalidated when worktree content fingerprint changes"
        )
        tamper_file.unlink()
        assert effective_acceptance(settings, tid) is not None, (
            "acceptance should be valid again after restoring worktree content"
        )

        # Invariant: New commit SHA in worktree invalidates acceptance
        (Path(wt1) / "profile.py").write_text("def render(): pass\n", encoding="utf-8")
        run_git(wt1, "add", "profile.py")
        run_git(wt1, "commit", "-m", "implement profile")
        new_sha = run_git(wt1, "rev-parse", "HEAD")
        assert new_sha != initial_sha

        transition(settings, tid, "implementing", stage="implementation", current_sha=new_sha)
        assert effective_acceptance(settings, tid) is None, (
            "acceptance should be invalidated after commit SHA change"
        )

        # 7. Repair cap enforcement (max_repair_rounds = 2)
        transition(settings, tid, "reviewing", stage="review")
        r1 = start_repair(settings, tid, ["repair issue 1"])
        assert r1["repair_round"] == 1
        assert r1["state"] == "implementing"

        transition(settings, tid, "reviewing", stage="review")
        r2 = start_repair(settings, tid, ["repair issue 2"])
        assert r2["repair_round"] == 2
        assert r2["state"] == "implementing"

        transition(settings, tid, "reviewing", stage="review")
        r3 = start_repair(settings, tid, ["repair issue 3"])
        # Strict invariant: exceeding max repair rounds transitions strictly to failed
        assert r3["state"] == "failed", f"expected failed on repair cap exceed, got {r3['state']}"
        assert r3["stage"] == "repair_limit_exceeded"
        assert get_task(settings, tid)["state"] == "failed"

        # 8. Task cancellation & lock release invariant
        t_cancel = create_task(settings, goal="Task to be cancelled", repo_path=str(repo_dir), base_sha=initial_sha)
        wt_cancel = create_worktree(settings, str(repo_dir), t_cancel["id"])
        dispatch_task(settings, t_cancel["id"], worktree_path=wt_cancel, base_sha=initial_sha)
        assert has_active_task_for_worktree(settings, wt_cancel) is True

        cancelled = cancel_task(settings, t_cancel["id"], reason="operator requested cancellation")
        assert cancelled["state"] == "cancelled"
        assert get_task(settings, t_cancel["id"])["state"] == "cancelled"

        # Invariant: cancelling releases worktree lock so another task can acquire it
        assert has_active_task_for_worktree(settings, wt_cancel) is False
        acquire_worktree_lock(settings, wt_cancel, "subsequent-task")
        release_worktree_lock(settings, wt_cancel, "subsequent-task")

        # 9. Restart recovery invariant
        recovered = recover_on_startup(settings)
        assert isinstance(recovered, list)

        # 10. Deployment state tracking (database store tracking ONLY)
        # Note: In this build, deployment tracking is strictly store record tracking in SQLite;
        # no automated network transfer (e.g. Tailscale) or remote execution is performed.
        run = create_deployment(
            settings,
            task_id=tid,
            source_machine="dev-mac",
            target_machine="prod-vps",
            build_version="v1.0.0",
            live_version="v0.9.0",
        )
        rid = run["id"]
        update_deployment_stage(settings, rid, "local_ci", "success", detail="unit tests verified")
        update_deployment_stage(settings, rid, "tailscale_transfer", "running")

        dep = get_deployment(settings, rid)
        local_ci_stage = next(s for s in dep["stages"] if s["stage"] == "local_ci")
        assert local_ci_stage["status"] == "success"
        ts_stage = next(s for s in dep["stages"] if s["stage"] == "tailscale_transfer")
        assert ts_stage["status"] == "running"

        # Failed deployment stage tracking
        update_deployment_stage(
            settings,
            rid,
            "tailscale_transfer",
            "failed",
            detail="unconfigured automated runner; manual step required",
        )
        dep2 = get_deployment(settings, rid)
        assert dep2["status"] == "failed"

        # 11. Task summary formatting
        task_summary = summary(settings, tid)
        assert "任务:" in task_summary

        print("agent_supervisor_smoke: PASS (offline invariants verified)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
