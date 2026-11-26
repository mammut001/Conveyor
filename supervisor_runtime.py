"""Own live Supervisor executions on one asyncio loop."""
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

import agent_supervisor as supervisor
from redaction import redact_text, truncate
from supervisor_validation import infer_validation


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(path), *args], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise supervisor.SupervisorStateError(f"cannot inspect Git workspace: {exc}") from exc
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _workspace(repo_path: str, worktree_path: str) -> tuple[str, str, str]:
    if not worktree_path:
        raise supervisor.SupervisorStateError("an existing isolated worktree is required")
    repo, worktree = Path(repo_path).resolve(), Path(worktree_path).resolve()
    if Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise supervisor.SupervisorStateError("worktree_path must name the Git worktree root")
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() == worktree:
        raise supervisor.SupervisorStateError("use a separate worktree, not the repository checkout")

    def common_dir(path: Path) -> Path:
        value = Path(_git(path, "rev-parse", "--git-common-dir"))
        return (path / value).resolve() if not value.is_absolute() else value.resolve()

    if common_dir(repo) != common_dir(worktree):
        raise supervisor.SupervisorStateError("worktree does not belong to the requested repository")
    if _git(worktree, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise supervisor.SupervisorStateError("worktree must be clean before dispatch")
    supervisor._workspace_snapshot(str(worktree))
    return str(worktree), _git(worktree, "rev-parse", "--verify", "HEAD"), _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")


def _create_worktree(settings: Any, task: dict) -> str:
    """Create a detached checkout of the repository's committed HEAD for this task."""
    task_id = task["id"]
    if not re.fullmatch(r"sup-[0-9]+-[0-9a-f]{8}", task_id):
        raise supervisor.SupervisorStateError("invalid supervisor task ID")
    repo = Path(task["repo_path"]).resolve()
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise supervisor.SupervisorStateError("repo_path must name a Git repository root")
    root = Path(settings.codex_task_root).resolve() / "supervisor-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    target = root / task_id
    if target.exists() or target.is_symlink():
        raise supervisor.SupervisorStateError(f"task worktree already exists: {target}")
    try:
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), "HEAD"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise supervisor.SupervisorStateError(f"cannot create task worktree: {exc}") from exc
    return str(target)


class SupervisorRuntime:
    def __init__(self, settings: Any):
        self.settings = settings
        self.running: dict[str, asyncio.Task] = {}

    async def dispatch(self, task_id: str, worktree_path: str = "", acceptance_criteria: str = "",
                       validation_commands: Any = None) -> dict:
        if not supervisor.enabled(self.settings):
            raise supervisor.SupervisorStateError("agent supervisor is disabled")
        task = supervisor.get_task(self.settings, task_id)
        if not task:
            raise supervisor.SupervisorStateError(f"task not found: {task_id}")
        if task["state"] != "pending_dispatch":
            raise supervisor.SupervisorStateError(f"task cannot be dispatched from {task['state']}")
        if validation_commands is None:
            validation_commands = (task.get("metadata") or {}).get("validation_commands")
        if validation_commands is not None and (not isinstance(validation_commands, list) or not 1 <= len(validation_commands) <= 8
                or any(not isinstance(command, list) or not 1 <= len(command) <= 32
                       or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\0" in arg
                              for arg in command) for command in validation_commands)):
            raise supervisor.SupervisorStateError("provide 1–8 validation_commands as non-empty argv arrays")
        roles = supervisor.load_roles(self.settings)
        for name in ("analyst", "implementer", "reviewer"):
            role = roles[name]
            available, reason = supervisor.get_executor(self.settings, role.executor).available()
            if not available:
                raise supervisor.SupervisorStateError(f"{name} executor unavailable: {reason}")
        requested_path = worktree_path or task["worktree_path"]
        if not requested_path:
            requested_path = _create_worktree(self.settings, task)
            supervisor.remember_pending_worktree(self.settings, task_id, requested_path)
        path, head, branch = _workspace(task["repo_path"], requested_path)
        validation_source = "operator"
        if validation_commands is None:
            inferred = infer_validation(path)
            if inferred is None:
                raise supervisor.SupervisorStateError(
                    "could not determine a project test command; provide validation_commands")
            validation_commands, validation_source = inferred
        supervisor.dispatch_task(self.settings, task_id, worktree_path=path, branch=branch, base_sha=head)
        supervisor.record_handoff(self.settings, task_id, from_role="system", to_role="analyst",
                                  summary=redact_text(f"Validation selected from {validation_source}: {validation_commands}"),
                                  sha=head, evidence_path="validation-selection")
        worker = asyncio.create_task(self._run(task_id, task["goal"], path, acceptance_criteria, validation_commands))
        self.running[task_id] = worker
        # Allow the wrapper to begin before a subsequent cancellation can arrive.
        await asyncio.sleep(0)
        return supervisor.get_task(self.settings, task_id) or {}

    async def _run(self, task_id: str, goal: str, path: str, criteria: str, commands: list[list[str]]) -> None:
        try:
            await supervisor.run_supervisor_flow(self.settings, task_id, goal=goal, worktree_path=path,
                                                 acceptance_criteria=criteria, already_dispatched=True,
                                                 validation_commands=commands)
        except asyncio.CancelledError:
            # The executor must have reaped its process group before propagating cancellation.
            supervisor.transition(self.settings, task_id, "cancelled", stage="cancelled", error="execution stopped")
            supervisor.release_worktree_lock(self.settings, path, task_id)
        except Exception as exc:
            # Do not unlock when the executor's process state is uncertain.
            supervisor.transition(self.settings, task_id, "interrupted", stage="execution_error",
                                  error=truncate(redact_text(str(exc)), 2000))
        finally:
            self.running.pop(task_id, None)

    async def cancel(self, task_id: str) -> dict:
        worker = self.running.get(task_id)
        if worker is None:
            return supervisor.cancel_task(self.settings, task_id)
        worker.cancel()
        await worker
        return supervisor.get_task(self.settings, task_id) or {}

    async def shutdown(self) -> None:
        workers = list(self.running.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
