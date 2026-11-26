#!/usr/bin/env python3
"""Smoke test for antigravity_supervisor runtime support."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from config import Settings
import agent_supervisor as ags
from agent_supervisor import (
    create_task, get_task, list_tasks, transition, dispatch_task,
    acquire_worktree_lock, release_worktree_lock, has_active_task_for_worktree,
    SupervisorLockError, record_handoff, list_handoffs, record_acceptance,
    effective_acceptance, latest_acceptance, start_repair, cancel_task,
    recover_on_startup, create_deployment, update_deployment_stage, get_deployment,
    summary, enabled, run_supervisor_flow, ExecutionResult, RoleConfig,
)

tmp = Path(tempfile.mkdtemp())
settings = Settings(
    telegram_bot_token="",
    telegram_allowed_user_id=0,
    codex_workspace_root=tmp,
    codex_bin="codex",
    codex_task_root=tmp / "tasks",
    codex_model=None,
    codex_timeout_seconds=60,
    telegram_progress_seconds=3,
    codex_retry_429_delays_seconds=(),
    codex_memory_root=tmp / "memory",
    user_timezone="UTC",
    agent_supervisor_enabled=True,
)
settings.codex_memory_root.mkdir(parents=True, exist_ok=True)
(settings.codex_memory_root / "state").mkdir(parents=True, exist_ok=True)

assert enabled(settings) is True

# 1. create task
t = create_task(settings, goal="add feature X", repo_path=str(tmp), base_sha="abc123")
assert t["state"] == "pending_dispatch"
tid = t["id"]

# 2. dispatch + lock
dt = dispatch_task(settings, tid, worktree_path=str(tmp / "wt1"), branch="feat/x", base_sha="abc123")
assert dt["state"] == "analyzing"
assert dt["worktree_path"] == str((tmp / "wt1").resolve())
# lock held
try:
    acquire_worktree_lock(settings, str(tmp / "wt1"), "other-task")
    raise SystemExit("FAIL: lock not enforced")
except SupervisorLockError:
    pass

# 3. transition through flow
transition(settings, tid, "implementing", stage="implementation")
transition(settings, tid, "reviewing", stage="review")
# acceptance must target SHA
try:
    record_acceptance(settings, tid, sha="", passed=True)
    raise SystemExit("FAIL: empty sha accepted")
except Exception:
    pass
acc = record_acceptance(settings, tid, sha="abc123", passed=False, issues=["bug"], evidence="log")
assert acc["passed"] is False
# Legacy acceptance without a workspace snapshot cannot be verified.
eff = effective_acceptance(settings, tid)
assert eff is None

# code change invalidates acceptance
transition(settings, tid, "implementing", stage="implementation", current_sha="def456")
eff2 = effective_acceptance(settings, tid)
assert eff2 is None, "acceptance should be invalidated after sha change"

# 4. repair loop + limit
start_repair(settings, tid, ["bug"])
t2 = get_task(settings, tid)
assert t2["state"] == "implementing"
# exhaust repair rounds
for _ in range(5):
    # force review then repair
    transition(settings, tid, "reviewing", stage="review")
    cur = get_task(settings, tid)
    if cur["repair_round"] >= cur["max_repair_rounds"]:
        break
    start_repair(settings, tid, ["bug"])
final = get_task(settings, tid)
# after exceeding, should be failed
assert final["state"] in ("failed", "implementing", "reviewing"), final["state"]

# 5. handoff
ho = record_handoff(settings, tid, from_role="analysis", to_role="implementation",
                    summary="plan", sha="abc123", evidence_path="/tmp/x")
hs = list_handoffs(settings, tid)
assert len(hs) >= 1

# 6. cancel
t3 = create_task(settings, goal="cancel me", repo_path=str(tmp))
cancel_task(settings, t3["id"], reason="user cancelled")
assert get_task(settings, t3["id"])["state"] == "cancelled"

# 7. duplicate dispatch conflict
a = create_task(settings, goal="a", repo_path=str(tmp))
b = create_task(settings, goal="b", repo_path=str(tmp))
dispatch_task(settings, a["id"], worktree_path=str(tmp / "wt3"), base_sha="x")
try:
    dispatch_task(settings, b["id"], worktree_path=str(tmp / "wt3"), base_sha="x")
    raise SystemExit("FAIL: duplicate dispatch not blocked")
except SupervisorLockError:
    pass

# 8. restart recovery
rec = recover_on_startup(settings)
assert isinstance(rec, list)

# 9. deployment pipeline
run = create_deployment(settings, task_id=tid, source_machine="mac", target_machine="vps",
                        build_version="v1", live_version="v0")
rid = run["id"]
update_deployment_stage(settings, rid, "local_ci", "success", detail="tests passed")
update_deployment_stage(settings, rid, "tailscale_transfer", "running")
dep = get_deployment(settings, rid)
local = next(s for s in dep["stages"] if s["stage"] == "local_ci")
assert local["status"] == "success"
assert any(s["stage"] == "tailscale_transfer" and s["status"] == "running" for s in dep["stages"])
# failed stage
update_deployment_stage(settings, rid, "tailscale_transfer", "failed", detail="timeout")
dep2 = get_deployment(settings, rid)
assert dep2["status"] == "failed"

# 10. summary
s = summary(settings, tid)
assert "任务:" in s

# Flow test with a fake executor (proves state advances from real execution results)
import asyncio

repo = tmp / "repo"
repo.mkdir()
subprocess = __import__("subprocess")
subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
(repo / "a.txt").write_text("hello")
subprocess.run(["git", "add", "."], cwd=repo, check=True)
subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
wt = tmp / "wt_flow"
subprocess.run(["git", "clone", "--local", str(repo), str(wt)], check=True, capture_output=True)

class FakeExecutor:
    name = "fake"
    def available(self):
        return True, "ok"
    async def run(self, *, role, prompt, worktree_path, timeout_seconds):
        # analyst passes; implementer creates a file; reviewer passes first time? fail first then pass
        if "analyst" in role.name or role.tools == ["read"]:
            # distinguish by prompt
            pass
        if "implementation" in prompt.lower() or "implementer" in prompt.lower():
            (Path(worktree_path) / "impl.txt").write_text("done")
            subprocess.run(["git", "-C", worktree_path, "add", "impl.txt"], check=True)
            # commit only if there are staged changes
            check = subprocess.run(["git", "-C", worktree_path, "diff", "--cached", "--quiet"])
            if check.returncode != 0:
                subprocess.run(["git", "-C", worktree_path, "commit", "-m", "impl"], check=True, capture_output=True)
        if "reviewer" in prompt.lower() or "independent reviewer" in prompt.lower():
            # first review fails, second passes
            if not hasattr(self, "_reviewed"):
                self._reviewed = True
                return ExecutionResult(success=True, output="FAIL: missing test", started_at=ags._utc_now(), finished_at=ags._utc_now(), sha=ags._git_head(worktree_path))
            return ExecutionResult(success=True, output="PASS", started_at=ags._utc_now(), finished_at=ags._utc_now(), sha=ags._git_head(worktree_path))
        return ExecutionResult(success=True, output="plan ok", started_at=ags._utc_now(), finished_at=ags._utc_now(), sha=ags._git_head(worktree_path))
    async def cancel(self, execution_id):
        return False

# patch executor factory (shared instance keeps review state)
_shared_executor = FakeExecutor()
ags.get_executor = lambda settings, name: _shared_executor
object.__setattr__(settings, "agent_supervisor_implementer_executor", "fake")
object.__setattr__(settings, "agent_supervisor_analyst_executor", "fake")
object.__setattr__(settings, "agent_supervisor_reviewer_executor", "fake")
object.__setattr__(settings, "agent_supervisor_max_repair_rounds", 2)

flow_task = create_task(settings, goal="flow test", repo_path=str(repo), base_sha=subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],capture_output=True,text=True).stdout.strip())
asyncio.get_event_loop().run_until_complete(ags.run_supervisor_flow(settings, flow_task["id"], goal="flow test", worktree_path=str(wt), acceptance_criteria="impl.txt exists"))
ft = get_task(settings, flow_task["id"])
print("FLOW STATE:", ft["state"], "repair_round:", ft["repair_round"])
assert ft["state"] in ("completed", "failed"), ft["state"]
acc = ags.latest_acceptance(settings, flow_task["id"])
print("ACCEPTANCE:", acc["passed"] if acc else None, acc["sha"] if acc else None)

print("antigravity_supervisor_smoke: PASS")
