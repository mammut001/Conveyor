"""runner/operators/jobs.py — job inspection plus Apply/Discard lifecycle."""
from __future__ import annotations

import asyncio
from pathlib import Path

from redaction import redact_text, truncate

MEMORY_FILENAME = "MEMORY.md"


def status_text(self) -> str:
    job = self.current_job or self.last_job
    if not job:
        return "No jobs yet."
    elapsed = self._elapsed(job)
    parts = [
        f"Job: {job.id}",
        f"Mode: /{job.mode.value}",
        f"State: {job.state.value}",
        f"Sandbox: {job.sandbox}",
        f"Attempt: {job.attempt}/{job.max_attempts}",
        f"Elapsed: {elapsed}",
        f"Last event: {truncate(job.last_event, 500)}",
    ]
    if job.log_path:
        parts.append(f"Log: {job.log_path}")
    if job.summary:
        parts.append(f"Summary: {truncate(job.summary, 1200)}")
    if job.error:
        parts.append(f"Error: {truncate(job.error, 1200)}")
    return "\n".join(parts)


async def diff_text(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    return truncate(await diff_job(self, job_id, worktree_path), 3900)


async def diff_job(self, job_id: str | None, worktree_path: Path | None) -> str:
    if not worktree_path or not worktree_path.exists():
        return "No job worktree available yet."
    status = await self._git(["status", "--short"], cwd=worktree_path, check=False)
    stat = await self._git(["diff", "--stat"], cwd=worktree_path, check=False)
    diff = await self._git(["diff", "--", "."], cwd=worktree_path, check=False)
    if not status.strip() and not stat.strip() and not diff.strip():
        return f"Job {job_id}: no git diff."
    return (
        f"Job {job_id} status:\n{status.strip() or '(clean)'}\n\n"
        f"Diff stat:\n{stat.strip() or '(no tracked changes)'}\n\n"
        f"Unified diff:\n{diff.strip() or '(no tracked diff; check untracked files above)'}"
    )[:1_000_000]


def jobs_text(self, limit: int = 8) -> str:
    records = self.job_records(limit, lane=getattr(self, "lane", "default"))
    if not records:
        return "No jobs yet."
    lines = ["Recent jobs:"]
    for record in records:
        preview = f" — {record.final_preview}" if record.final_preview else ""
        lines.append(f"{record.id} · {record.state}{preview}")
    return truncate("\n".join(lines), 3900)


def last_text(self) -> str:
    record = self.job_records(1, lane=getattr(self, "lane", "default"))
    if not record:
        return "No jobs yet."
    item = record[0]
    if item.final_preview:
        return item.final_preview
    return f"{item.id}: {item.state}"


def _active_refinement(self, worktree_path: Path | None):
    if not worktree_path:
        return None, None
    try:
        from refinement_store import RefinementStore
        store = RefinementStore(self.settings)
        return store, store.active_for_worktree(worktree_path)
    except Exception as exc:
        # Shared refinement worktrees can be mutated by a later queued/running
        # turn. If the control-plane DB cannot tell us whether this path is
        # active, Apply/Discard must not silently fall back to legacy behavior
        # and race that writer. Fail closed until refinement state is readable.
        raise RuntimeError(
            "Refinement state is unavailable; refusing to Apply/Discard this worktree."
        ) from exc


def _emit_refinement_closed(self, chain: dict | None, *, state: str, reason: str) -> None:
    if not chain:
        return
    job_id = str(chain.get("latest_queue_job_id") or chain.get("root_queue_job_id") or "")
    if not job_id:
        return
    try:
        from agent_events import emit_event
        emit_event(
            self.settings,
            "refinement.closed",
            job_id,
            {
                "chain_id": chain.get("id"),
                "state": state,
                "turn_count": chain.get("turn_count"),
                "reason": reason,
            },
            session_id=chain.get("source_chat_id"),
        )
    except Exception:
        pass


async def discard_last_job(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    return await discard_job(self, job_id, worktree_path)


async def discard_job(self, job_id: str | None, worktree_path: Path | None) -> str:
    if not worktree_path or not worktree_path.exists():
        return "No job worktree to discard."

    try:
        store, active = _active_refinement(self, worktree_path)
    except RuntimeError as exc:
        return str(exc)
    guard = store.begin_mutation(worktree_path) if active and store is not None else None
    if guard is not None and guard.running_conflict:
        return "A refinement job is still running in this worktree. Cancel or let it finish before Discard."

    closed = None
    try:
        if guard is not None and guard.active:
            # Commit the closed state while holding the same SQLite write lock
            # used by queue dequeue. A queued follow-up can only start after
            # this commit and will therefore fail closed instead of touching
            # the path while it is being removed.
            closed = guard.close(state="discarded", reason=f"discarded from {job_id}")
            guard = None
        await self._remove_worktree(worktree_path)
    finally:
        if guard is not None:
            guard.release()

    _emit_refinement_closed(self, closed, state="discarded", reason="discard")
    return f"Discarded worktree for {job_id}."


async def apply_last_job(self) -> str:
    worktree_path = self._last_worktree_path()
    job_id = self._last_job_id()
    return await apply_job(self, job_id, worktree_path)


async def apply_job(self, job_id: str | None, worktree_path: Path | None) -> str:
    # apply.lock is a file lock held across awaits; the gate keeps a second
    # Apply in this process from blocking the event loop on it (async_gate).
    from runner.file_lock import async_gate

    async with async_gate("apply"):
        return await _apply_job_locked(self, job_id, worktree_path)


async def _apply_job_locked(self, job_id: str | None, worktree_path: Path | None) -> str:
    from runner.file_lock import file_lock
    from runner.apply_policy import (
        validate_apply_paths,
        collect_tracked_changed_files,
        collect_untracked_files,
    )

    if not worktree_path or not worktree_path.exists():
        return "No job worktree to apply."
    # The repository the worktree belongs to: the configured workspace, or
    # the project folder of the agent whose job this is.
    from runner.worktree import _repo_root_for
    apply_root = await _repo_root_for(self, worktree_path)

    try:
        store, active = _active_refinement(self, worktree_path)
    except RuntimeError as exc:
        return str(exc)
    lock_path = self.settings.codex_task_root / "locks" / "apply.lock"
    with file_lock(lock_path):
        # Acquire after apply.lock so every Apply process uses the same lock
        # ordering. BEGIN IMMEDIATE blocks queue dequeue/enqueue from racing
        # between the idle check and a successful close. Any failed policy
        # path rolls back, keeping the chain active.
        guard = store.begin_mutation(worktree_path) if active and store is not None else None
        if guard is not None and guard.running_conflict:
            return "A refinement job is still running in this worktree. Wait for it to finish before Apply."

        try:
            root_status = await self._git(
                ["status", "--short"], cwd=apply_root, check=False
            )
            if root_status.strip():
                return "Main workspace has uncommitted changes. I will not apply over a dirty repo."

            tracked_result = collect_tracked_changed_files(worktree_path)
            if not tracked_result.ok:
                return f"Refused to apply job {job_id}: could not collect changed files safely."
            untracked_result = collect_untracked_files(worktree_path)
            if not untracked_result.ok:
                return f"Refused to apply job {job_id}: could not collect changed files safely."

            tracked_files = tracked_result.paths
            untracked_files = untracked_result.paths

            if tracked_files:
                val_tracked = validate_apply_paths(
                    tracked_files,
                    kind="tracked",
                    settings=self.settings, workspace_root=apply_root,
                    worktree_path=worktree_path,
                )
                if not val_tracked.allowed:
                    return (
                        f"Refused to apply job {job_id}: blocked high-risk paths: "
                        f"{val_tracked.reason}"
                    )

            if untracked_files:
                val_untracked = validate_apply_paths(
                    untracked_files,
                    kind="untracked",
                    settings=self.settings, workspace_root=apply_root,
                    worktree_path=worktree_path,
                )
                if not val_untracked.allowed:
                    return (
                        f"Refused to apply job {job_id}: blocked high-risk paths: "
                        f"{val_untracked.reason}"
                    )

            validated_untracked = set(untracked_files)
            memory_pathspec = f":(exclude){MEMORY_FILENAME}"
            if not tracked_files and not untracked_files:
                return f"Job {job_id} has no changes to apply."

            patch = await self._git(
                ["diff", "--binary", "HEAD", "--", ".", memory_pathspec],
                cwd=worktree_path, check=False,
            )

            # Preflight all destinations before modifying the target checkout.
            # Target collisions and symlinked parents must be rejected up front.
            root_real = apply_root.resolve()
            tree_real = worktree_path.resolve()
            for relative in untracked_files:
                target = apply_root / relative
                source = worktree_path / relative
                if target.exists() or target.is_symlink():
                    return f"Refused to apply job {job_id}: untracked target exists: {relative}"
                if root_real not in target.parent.resolve().parents and target.parent.resolve() != root_real:
                    return f"Refused to apply job {job_id}: unsafe target parent: {relative}"
                if tree_real not in source.parent.resolve().parents and source.parent.resolve() != tree_real:
                    return f"Refused to apply job {job_id}: unsafe source parent: {relative}"

            async def _apply_patch(*, reverse: bool = False, check_only: bool = False):
                flags = ["--binary"]
                if reverse:
                    flags.append("--reverse")
                if check_only:
                    flags.append("--check")
                proc = await asyncio.create_subprocess_exec(
                    "git", "apply", *flags, "-",
                    cwd=apply_root, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate(patch.encode("utf-8"))
                return proc.returncode, truncate(
                    (stderr or stdout).decode("utf-8", errors="replace").strip(), 1200
                )

            if patch.strip():
                root_status_pre = await self._git(
                    ["status", "--short"], cwd=apply_root, check=False
                )
                if root_status_pre.strip():
                    return "Main workspace has uncommitted changes. I will not apply over a dirty repo."
                code, detail = await _apply_patch(check_only=True)
                if code != 0:
                    return f"Refused to apply job {job_id}: preflight patch failed: {detail}"

            patch_applied = False
            copied_paths: list[str] = []
            try:
                if patch.strip():
                    code, detail = await _apply_patch()
                    if code != 0:
                        return f"Could not apply tracked diff for {job_id}: {detail}"
                    patch_applied = True

                recheck_result = collect_untracked_files(worktree_path)
                if not recheck_result.ok:
                    raise RuntimeError("could not collect untracked files safely")
                if set(recheck_result.paths) != validated_untracked:
                    raise RuntimeError("untracked files changed during Apply")

                copied = await self._copy_validated_untracked_files(
                    worktree_path, list(validated_untracked)
                )
                copied_paths = list(validated_untracked)
                status_summary = await self._git(
                    ["status", "--short"], cwd=apply_root, check=False
                )
                safe_summary = redact_text(status_summary.strip())

                closed = None
                if guard is not None and guard.active:
                    closed = guard.close(state="applied", reason=f"applied from {job_id}")
                    guard = None
                _emit_refinement_closed(self, closed, state="applied", reason="apply")
                return (
                    f"Applied {job_id}. Copied {copied} new files. Review main repo before committing.\n\n"
                    f"Workspace status:\n{safe_summary}"
                )
            except Exception as exc:
                # The copier handles partial-copy failures itself. If a later
                # step (e.g. DB close) fails, remove only completed files that
                # still match what we copied; never delete concurrent edits.
                untracked_rollback_ok = True
                for relative in copied_paths:
                    target = apply_root / relative
                    source = worktree_path / relative
                    try:
                        if target.is_file() and not target.is_symlink() and target.read_bytes() == source.read_bytes():
                            target.unlink()
                        else:
                            untracked_rollback_ok = False
                    except OSError:
                        untracked_rollback_ok = False
                # Undo tracked patch *only*, rather than git reset --hard:
                # the latter could overwrite concurrent user changes.
                rollback = "no tracked patch to revert"
                if patch_applied:
                    try:
                        code, detail = await _apply_patch(reverse=True)
                        rollback = "tracked diff reversed" if code == 0 else f"FAILED ({detail})"
                    except Exception as rollback_error:
                        rollback = f"FAILED ({type(rollback_error).__name__})"
                if rollback.startswith("FAILED") or not untracked_rollback_ok:
                    return (
                        f"Apply failed for {job_id}; tracked rollback: {rollback}; "
                        f"untracked cleanup: {'ok' if untracked_rollback_ok else 'FAILED'}. "
                        "Main workspace requires manual review before retrying."
                    )
                return (
                    f"Apply failed for {job_id}: {type(exc).__name__}. "
                    f"Rollback: {rollback}. Refinement remains open."
                )
        finally:
            if guard is not None:
                guard.release()
