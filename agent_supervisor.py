"""agent_supervisor.py — Generic, executor-pluggable supervisor runtime.

This is the generic core.  ``antigravity_supervisor.py`` and ``supervisor.py``
are compatibility shims that re-export it.  Antigravity is just one optional
executor adapter; nothing here assumes it exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import stat
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from redaction import redact_text, truncate

TASK_STATES = (
    "pending_dispatch",
    "analyzing",
    "implementing",
    "reviewing",
    "repair_pending",
    "completed",
    "failed",
    "cancelled",
    "waiting_user",
    "interrupted",
)

DEPLOYMENT_STAGES = (
    "local_ci",
    "tailscale_transfer",
    "vps_verify",
    "service_switch",
    "health_check",
)

ROLES = ("coordinator", "analyst", "implementer", "reviewer")

_LOCK = threading.Lock()


class SupervisorStateError(ValueError):
    pass


class SupervisorLockError(RuntimeError):
    pass


@dataclass
class RoleConfig:
    name: str
    executor: str = "claude"
    model: str | None = None
    tools: list[str] = field(default_factory=lambda: ["read"])
    location: str = "control_plane"  # control_plane | worktree


@dataclass
class ExecutionResult:
    success: bool
    output: str = ""
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    sha: str = ""
    cancelled: bool = False


class Executor(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    async def run(
        self,
        *,
        role: RoleConfig,
        prompt: str,
        worktree_path: str,
        timeout_seconds: int,
    ) -> ExecutionResult: ...

    async def cancel(self, execution_id: str) -> bool: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(settings: Any) -> Path:
    root = Path(getattr(settings, "codex_memory_root", Path.home() / ".codex"))
    (root / "state").mkdir(parents=True, exist_ok=True)
    return root


def _db_path(settings: Any) -> Path:
    return _root(settings) / "state" / "agent_supervisor.sqlite3"


def _connect(settings: Any) -> sqlite3.Connection:
    path = _db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS supervisor_tasks (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            worktree_path TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            base_sha TEXT NOT NULL DEFAULT '',
            current_sha TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT '',
            coordinator_model TEXT NOT NULL DEFAULT '',
            analyst_model TEXT NOT NULL DEFAULT '',
            implementer_executor TEXT NOT NULL DEFAULT '',
            reviewer_model TEXT NOT NULL DEFAULT '',
            repair_round INTEGER NOT NULL DEFAULT 0,
            max_repair_rounds INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_state ON supervisor_tasks(state);
        CREATE INDEX IF NOT EXISTS idx_supervisor_tasks_worktree ON supervisor_tasks(worktree_path);

        CREATE TABLE IF NOT EXISTS supervisor_handoffs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            from_role TEXT NOT NULL,
            to_role TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            sha TEXT NOT NULL DEFAULT '',
            evidence_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES supervisor_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_supervisor_handoffs_task ON supervisor_handoffs(task_id, created_at);

        CREATE TABLE IF NOT EXISTS supervisor_acceptance (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            sha TEXT NOT NULL,
            passed INTEGER NOT NULL,
            issues_json TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES supervisor_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_supervisor_acceptance_task ON supervisor_acceptance(task_id, created_at);

        CREATE TABLE IF NOT EXISTS supervisor_locks (
            worktree_path TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS supervisor_deployments (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            source_machine TEXT NOT NULL DEFAULT '',
            target_machine TEXT NOT NULL DEFAULT '',
            build_version TEXT NOT NULL DEFAULT '',
            live_version TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_supervisor_deployments_task ON supervisor_deployments(task_id);

        CREATE TABLE IF NOT EXISTS supervisor_deployment_stages (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(run_id) REFERENCES supervisor_deployments(id)
        );
        CREATE INDEX IF NOT EXISTS idx_supervisor_deployment_stages_run ON supervisor_deployment_stages(run_id, stage);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(supervisor_acceptance)")}
    if "snapshot" not in columns:
        try:
            conn.execute("ALTER TABLE supervisor_acceptance ADD COLUMN snapshot TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            # Another connection may have completed the migration concurrently.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(supervisor_acceptance)")}
            if "snapshot" not in columns:
                raise exc
    conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "metadata_json" in data and isinstance(data["metadata_json"], str):
        try:
            data["metadata"] = json.loads(data["metadata_json"] or "{}")
        except Exception:
            data["metadata"] = {}
    if "issues_json" in data and isinstance(data.get("issues_json"), str):
        try:
            data["issues"] = json.loads(data["issues_json"] or "[]")
        except Exception:
            data["issues"] = []
    if "passed" in data:
        data["passed"] = bool(data["passed"])
    return data


def enabled(settings: Any) -> bool:
    # Generic flag first, then legacy antigravity flag for compatibility.
    if getattr(settings, "agent_supervisor_enabled", None) is not None:
        if getattr(settings, "agent_supervisor_enabled", False):
            return True
    return bool(getattr(settings, "antigravity_supervisor_enabled", False))


def max_repair_rounds(settings: Any) -> int:
    try:
        return max(1, int(getattr(settings, "agent_supervisor_max_repair_rounds",
                                 getattr(settings, "supervisor_max_repair_rounds", 3)) or 3))
    except (TypeError, ValueError):
        return 3


def task_timeout(settings: Any) -> int:
    try:
        return max(60, int(getattr(settings, "agent_supervisor_task_timeout_seconds",
                                   getattr(settings, "supervisor_task_timeout_seconds", 3600)) or 3600))
    except (TypeError, ValueError):
        return 3600


# ---------------------------------------------------------------------------
# Config compatibility: read generic or legacy fields
# ---------------------------------------------------------------------------

def _field(settings: Any, generic: str, legacy: str, default: Any = None) -> Any:
    v = getattr(settings, generic, None)
    if v is None:
        v = getattr(settings, legacy, None)
    return default if v is None else v


def chat_model(settings: Any) -> str | None:
    return _field(settings, "agent_supervisor_chat_model", "supervisor_chat_model",
                  getattr(settings, "chat_model", None))


def analysis_model(settings: Any) -> str | None:
    return _field(settings, "agent_supervisor_analysis_model", "supervisor_analysis_model",
                  getattr(settings, "codex_model", None))


def implementation_executor_name(settings: Any) -> str:
    return _field(settings, "agent_supervisor_implementation_executor", "supervisor_implementation_executor", "claude") or "claude"


# ---------------------------------------------------------------------------
# Role configuration
# ---------------------------------------------------------------------------

def load_roles(settings: Any) -> dict[str, RoleConfig]:
    """Load role configuration from JSON file or env defaults."""
    roles: dict[str, RoleConfig] = {}
    path = _root(settings) / "supervisor_roles.json"
    file_cfg: dict[str, Any] = {}
    if path.exists():
        try:
            file_cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            file_cfg = {}
    defaults = {
        "coordinator": RoleConfig("coordinator", executor="chat", tools=["read"], location="control_plane"),
        "analyst": RoleConfig("analyst", executor=implementation_executor_name(settings), tools=["read"], location="control_plane"),
        "implementer": RoleConfig("implementer", executor=implementation_executor_name(settings), tools=["read", "write", "bash"], location="worktree"),
        "reviewer": RoleConfig("reviewer", executor=implementation_executor_name(settings), tools=["read"], location="worktree"),
    }
    for name in ROLES:
        cfg = file_cfg.get(name, {}) if isinstance(file_cfg, dict) else {}
        base = defaults[name]
        roles[name] = RoleConfig(
            name=name,
            executor=str(cfg.get("executor", getattr(settings, f"agent_supervisor_{name}_executor", None) or base.executor)),
            model=cfg.get("model", getattr(settings, f"agent_supervisor_{name}_model", None) or base.model),
            tools=list(cfg.get("tools", base.tools)),
            location=str(cfg.get("location", base.location)),
        )
    # Model binding from generic settings if present
    if roles["analyst"].model is None:
        roles["analyst"].model = analysis_model(settings)
    if roles["reviewer"].model is None:
        roles["reviewer"].model = analysis_model(settings)
    if roles["implementer"].model is None:
        roles["implementer"].model = getattr(settings, "codex_model", None)
    return roles


# ---------------------------------------------------------------------------
# Task lifecycle (same as before, generic columns)
# ---------------------------------------------------------------------------

def create_task(settings: Any, *, goal: str, repo_path: str, worktree_path: str = "",
                branch: str = "", base_sha: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if not goal or not str(goal).strip():
        raise SupervisorStateError("goal is required")
    if not repo_path:
        raise SupervisorStateError("repo_path is required")
    task_id = f"sup-{int(time.time() * 1000)}-{os.urandom(4).hex()}"
    now = _utc_now()
    meta = metadata or {}
    conn = _connect(settings)
    try:
        with conn:
            conn.execute(
                """INSERT INTO supervisor_tasks
                   (id, goal, repo_path, worktree_path, branch, base_sha, current_sha,
                    state, stage, coordinator_model, analyst_model, implementer_executor,
                    reviewer_model, repair_round, max_repair_rounds, created_at, updated_at, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, str(goal).strip(), str(repo_path), str(worktree_path or ""), str(branch or ""),
                 str(base_sha or ""), str(base_sha or ""), "pending_dispatch", "dispatch",
                 chat_model(settings) or "", analysis_model(settings) or "",
                 implementation_executor_name(settings), analysis_model(settings) or "",
                 0, max_repair_rounds(settings), now, now, json.dumps(meta, ensure_ascii=False)),
            )
    finally:
        conn.close()
    return get_task(settings, task_id) or {}


def get_task(settings: Any, task_id: str) -> dict[str, Any] | None:
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT * FROM supervisor_tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_tasks(settings: Any, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        if state:
            rows = conn.execute("SELECT * FROM supervisor_tasks WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                                (state, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM supervisor_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def remember_pending_worktree(settings: Any, task_id: str, path: str) -> None:
    """Retain an automatically created worktree when dispatch needs input."""
    conn = _connect(settings)
    try:
        with conn:
            result = conn.execute(
                "UPDATE supervisor_tasks SET worktree_path = ?, updated_at = ? "
                "WHERE id = ? AND state = 'pending_dispatch' AND worktree_path = ''",
                (path, _utc_now(), task_id),
            )
            if result.rowcount != 1:
                raise SupervisorStateError("task changed while its worktree was being prepared")
    finally:
        conn.close()


def transition(settings: Any, task_id: str, new_state: str, *, stage: str | None = None,
               error: str | None = None, current_sha: str | None = None,
               metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if new_state not in TASK_STATES:
        raise SupervisorStateError(f"unknown state: {new_state}")
    task = get_task(settings, task_id)
    if task is None:
        raise SupervisorStateError(f"task not found: {task_id}")
    now = _utc_now()
    fields: dict[str, Any] = {"state": new_state, "updated_at": now}
    if stage is not None:
        fields["stage"] = stage
    if error is not None:
        fields["error"] = error
    if current_sha is not None:
        fields["current_sha"] = current_sha
    if new_state in ("completed", "failed", "cancelled", "interrupted"):
        fields["finished_at"] = now
    if new_state in ("analyzing", "implementing", "reviewing") and not task.get("started_at"):
        fields["started_at"] = now
    if metadata:
        merged = dict(task.get("metadata") or {})
        merged.update(metadata)
        fields["metadata_json"] = json.dumps(merged, ensure_ascii=False)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect(settings)
    try:
        with conn:
            conn.execute(f"UPDATE supervisor_tasks SET {assignments} WHERE id = ?",
                         (*fields.values(), task_id))
    finally:
        conn.close()
    updated = get_task(settings, task_id) or {}
    if current_sha is not None and current_sha and current_sha != task.get("current_sha"):
        invalidate_acceptance(settings, task_id, reason="code version changed")
    return updated


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def acquire_worktree_lock(settings: Any, worktree_path: str, task_id: str, ttl_seconds: int = 3600) -> None:
    if not worktree_path:
        return
    worktree_path = str(Path(worktree_path).resolve())
    now = _utc_now()
    expires = datetime.fromtimestamp(time.time() + ttl_seconds, tz=timezone.utc).isoformat()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT task_id, expires_at FROM supervisor_locks WHERE worktree_path = ?",
                           (worktree_path,)).fetchone()
        if row is not None:
            if row["task_id"] != task_id:
                conn.rollback()
                raise SupervisorLockError(f"worktree {worktree_path} is already locked by task {row['task_id']}")
            conn.execute("DELETE FROM supervisor_locks WHERE worktree_path = ?", (worktree_path,))
        conn.execute("INSERT INTO supervisor_locks (worktree_path, task_id, acquired_at, expires_at) VALUES (?,?,?,?)",
                     (worktree_path, task_id, now, expires))
        conn.commit()
    except SupervisorLockError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_worktree_lock(settings: Any, worktree_path: str, task_id: str | None = None) -> None:
    if not worktree_path:
        return
    worktree_path = str(Path(worktree_path).resolve())
    conn = _connect(settings)
    try:
        with conn:
            if task_id:
                conn.execute("DELETE FROM supervisor_locks WHERE worktree_path = ? AND task_id = ?",
                             (worktree_path, task_id))
            else:
                conn.execute("DELETE FROM supervisor_locks WHERE worktree_path = ?", (worktree_path,))
    finally:
        conn.close()


def has_active_task_for_worktree(settings: Any, worktree_path: str, exclude_task_id: str | None = None) -> bool:
    if not worktree_path:
        return False
    worktree_path = str(Path(worktree_path).resolve())
    conn = _connect(settings)
    try:
        rows = conn.execute("SELECT id, state FROM supervisor_tasks WHERE worktree_path = ?",
                            (worktree_path,)).fetchall()
        for r in rows:
            if exclude_task_id and r["id"] == exclude_task_id:
                continue
            if r["state"] in ("pending_dispatch", "analyzing", "implementing", "reviewing", "repair_pending", "waiting_user"):
                return True
        return False
    finally:
        conn.close()


def dispatch_task(settings: Any, task_id: str, *, worktree_path: str | None = None,
                  branch: str | None = None, base_sha: str | None = None) -> dict[str, Any]:
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute("SELECT * FROM supervisor_tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise SupervisorStateError(f"task not found: {task_id}")
        if task["state"] != "pending_dispatch":
            raise SupervisorStateError(f"task cannot be dispatched from {task['state']}")
        wp = worktree_path if worktree_path is not None else task["worktree_path"]
        if wp:
            wp = str(Path(wp).resolve())
            row = conn.execute("SELECT task_id FROM supervisor_locks WHERE worktree_path = ?", (wp,)).fetchone()
            if row is not None:
                raise SupervisorLockError(f"worktree {wp} is locked by task {row['task_id']}")
            conn.execute("INSERT INTO supervisor_locks (worktree_path, task_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                         (wp, task_id, now, datetime.fromtimestamp(time.time() + 86400, tz=timezone.utc).isoformat()))
        try:
            updates = {"state": "analyzing", "stage": "analysis", "updated_at": now}
            updates["worktree_path"] = wp
            if branch is not None:
                updates["branch"] = branch
            if base_sha is not None:
                updates["base_sha"] = base_sha
                updates["current_sha"] = base_sha
            if not task["started_at"]:
                updates["started_at"] = now
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE supervisor_tasks SET {assignments} WHERE id = ?",
                         (*updates.values(), task_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_task(settings, task_id) or {}


# ---------------------------------------------------------------------------
# Handoffs / acceptance
# ---------------------------------------------------------------------------

def record_handoff(settings: Any, task_id: str, *, from_role: str, to_role: str,
                   summary: str, sha: str = "", evidence_path: str = "") -> dict[str, Any]:
    task = get_task(settings, task_id)
    if task is None:
        raise SupervisorStateError(f"task not found: {task_id}")
    handoff_id = f"ho-{os.urandom(8).hex()}"
    now = _utc_now()
    conn = _connect(settings)
    try:
        with conn:
            conn.execute("""INSERT INTO supervisor_handoffs
                (id, task_id, from_role, to_role, summary, sha, evidence_path, created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (handoff_id, task_id, from_role, to_role, summary, sha, evidence_path, now))
    finally:
        conn.close()
    return {"id": handoff_id, "task_id": task_id, "from_role": from_role, "to_role": to_role,
            "summary": summary, "sha": sha, "evidence_path": evidence_path, "created_at": now}


def list_handoffs(settings: Any, task_id: str) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        rows = conn.execute("SELECT * FROM supervisor_handoffs WHERE task_id = ? ORDER BY created_at ASC",
                            (task_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_acceptance(settings: Any, task_id: str, *, sha: str, passed: bool,
                      issues: list[str] | None = None, evidence: str = "",
                      snapshot: str = "") -> dict[str, Any]:
    task = get_task(settings, task_id)
    if task is None:
        raise SupervisorStateError(f"task not found: {task_id}")
    if not sha:
        raise SupervisorStateError("acceptance must target a specific SHA or snapshot")
    acc_id = f"acc-{os.urandom(8).hex()}"
    now = _utc_now()
    issues = issues or []
    conn = _connect(settings)
    try:
        with conn:
            conn.execute("""INSERT INTO supervisor_acceptance
                (id, task_id, sha, passed, issues_json, evidence, created_at, snapshot)
                VALUES (?,?,?,?,?,?,?,?)""",
                (acc_id, task_id, sha, 1 if passed else 0, json.dumps(issues, ensure_ascii=False), evidence, now, snapshot))
    finally:
        conn.close()
    return {"id": acc_id, "task_id": task_id, "sha": sha, "passed": passed,
            "issues": issues, "evidence": evidence, "created_at": now, "snapshot": snapshot}


def latest_acceptance(settings: Any, task_id: str) -> dict[str, Any] | None:
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT * FROM supervisor_acceptance WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                           (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["issues"] = json.loads(data.get("issues_json") or "[]")
        except Exception:
            data["issues"] = []
        data["passed"] = bool(data["passed"])
        return data
    finally:
        conn.close()


def effective_acceptance(settings: Any, task_id: str) -> dict[str, Any] | None:
    task = get_task(settings, task_id)
    if task is None:
        return None
    acc = latest_acceptance(settings, task_id)
    if acc is None:
        return None
    if acc.get("sha") != task.get("current_sha"):
        return None
    # A commit alone does not identify staged, unstaged or untracked changes.
    if not acc.get("snapshot") or not task.get("worktree_path"):
        return None
    try:
        if _workspace_snapshot(task["worktree_path"]) != acc["snapshot"]:
            return None
    except (OSError, subprocess.SubprocessError, SupervisorStateError):
        return None
    return acc


def invalidate_acceptance(settings: Any, task_id: str, reason: str = "") -> None:
    try:
        record_handoff(settings, task_id, from_role="system", to_role="reviewer",
                       summary=f"acceptance invalidated: {reason}".strip(), sha="", evidence_path="")
    except Exception:
        pass


def start_repair(settings: Any, task_id: str, issues: list[str]) -> dict[str, Any]:
    task = get_task(settings, task_id)
    if task is None:
        raise SupervisorStateError(f"task not found: {task_id}")
    if task.get("repair_round", 0) >= task.get("max_repair_rounds", 3):
        transition(settings, task_id, "failed", stage="repair_limit_exceeded",
                   error="max repair rounds exceeded; remaining issues reported")
        record_handoff(settings, task_id, from_role="reviewer", to_role="user",
                       summary="max repair rounds exceeded; remaining issues: " + "; ".join(issues),
                       sha=task.get("current_sha", ""))
        return get_task(settings, task_id) or {}
    conn = _connect(settings)
    try:
        with conn:
            conn.execute("UPDATE supervisor_tasks SET repair_round = repair_round + 1, updated_at = ? WHERE id = ?",
                         (_utc_now(), task_id))
    finally:
        conn.close()
    transition(settings, task_id, "repair_pending", stage="repair")
    transition(settings, task_id, "implementing", stage="implementation")
    return get_task(settings, task_id) or {}


def cancel_task(settings: Any, task_id: str, reason: str = "cancelled by user") -> dict[str, Any]:
    task = get_task(settings, task_id)
    if task is None:
        raise SupervisorStateError(f"task not found: {task_id}")
    if task["state"] in ("analyzing", "implementing", "reviewing", "repair_pending", "interrupted"):
        raise SupervisorStateError("active or interrupted execution must be stopped by its runtime before unlocking")
    release_worktree_lock(settings, task.get("worktree_path", ""), task_id)
    transition(settings, task_id, "cancelled", stage="cancelled", error=reason)
    return get_task(settings, task_id) or {}


def recover_on_startup(settings: Any) -> list[dict[str, Any]]:
    conn = _connect(settings)
    recovered: list[dict[str, Any]] = []
    try:
        rows = conn.execute("SELECT id, state, worktree_path FROM supervisor_tasks WHERE state IN "
                            "('analyzing','implementing','reviewing','repair_pending')").fetchall()
        for r in rows:
            transition(settings, r["id"], "interrupted", stage="recovered",
                       error="process restarted; execution status unknown; workspace remains locked")
            recovered.append(get_task(settings, r["id"]) or {})
    finally:
        conn.close()
    return recovered


# ---------------------------------------------------------------------------
# Deployment pipeline
# ---------------------------------------------------------------------------

def create_deployment(settings: Any, *, task_id: str | None = None, source_machine: str,
                      target_machine: str, build_version: str = "", live_version: str = "",
                      metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = f"dep-{os.urandom(8).hex()}"
    now = _utc_now()
    conn = _connect(settings)
    try:
        with conn:
            conn.execute("""INSERT INTO supervisor_deployments
                (id, task_id, source_machine, target_machine, build_version, live_version,
                 stage, status, started_at, metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, task_id, source_machine, target_machine, build_version, live_version,
                 "local_ci", "pending", now, json.dumps(metadata or {}, ensure_ascii=False)))
            for stage in DEPLOYMENT_STAGES:
                conn.execute("""INSERT INTO supervisor_deployment_stages (id, run_id, stage, status)
                    VALUES (?,?,?,?)""", (f"{run_id}:{stage}", run_id, stage, "pending"))
    finally:
        conn.close()
    return get_deployment(settings, run_id) or {}


def get_deployment(settings: Any, run_id: str) -> dict[str, Any] | None:
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT * FROM supervisor_deployments WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        stages = conn.execute("SELECT * FROM supervisor_deployment_stages WHERE run_id = ? ORDER BY rowid",
                              (run_id,)).fetchall()
        data["stages"] = [dict(s) for s in stages]
        try:
            data["metadata"] = json.loads(data.get("metadata_json") or "{}")
        except Exception:
            data["metadata"] = {}
        return data
    finally:
        conn.close()


def update_deployment_stage(settings: Any, run_id: str, stage: str, status: str, *, detail: str = "") -> dict[str, Any]:
    if stage not in DEPLOYMENT_STAGES:
        raise SupervisorStateError(f"unknown deployment stage: {stage}")
    if status not in ("pending", "running", "success", "failed", "skipped"):
        raise SupervisorStateError(f"unknown stage status: {status}")
    now = _utc_now()
    conn = _connect(settings)
    try:
        with conn:
            conn.execute("""UPDATE supervisor_deployment_stages
                SET status = ?, detail = ?, started_at = COALESCE(started_at, ?),
                    finished_at = CASE WHEN ? IN ('success','failed','skipped') THEN ? ELSE finished_at END
                WHERE run_id = ? AND stage = ?""",
                (status, detail, now, status, now, run_id, stage))
            run_status = "running"
            if status == "failed":
                run_status = "failed"
            elif status == "success" and stage == DEPLOYMENT_STAGES[-1]:
                run_status = "success"
            conn.execute("""UPDATE supervisor_deployments
                SET stage = ?, status = ?, finished_at = CASE WHEN ? IN ('success','failed') THEN ? ELSE finished_at END,
                    error = CASE WHEN ? = 'failed' THEN ? ELSE error END
                WHERE id = ?""", (stage, run_status, run_status, now, status, detail, run_id))
    finally:
        conn.close()
    return get_deployment(settings, run_id) or {}


def list_deployments(settings: Any, *, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        if task_id:
            rows = conn.execute("SELECT id FROM supervisor_deployments WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                                (task_id, limit)).fetchall()
        else:
            rows = conn.execute("SELECT id FROM supervisor_deployments ORDER BY started_at DESC LIMIT ?",
                                (limit,)).fetchall()
        return [get_deployment(settings, r["id"]) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _workspace_snapshot(worktree_path: str) -> str:
    """Identify HEAD, index, working changes and non-ignored untracked files.

    Refuse submodules: a parent Git diff does not identify their full contents.
    Ignored files are intentionally outside the reviewed source snapshot.
    """
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", worktree_path, *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout

    digest = hashlib.sha256()
    def add(label: bytes, data: bytes) -> None:
        digest.update(label + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)

    add(b"head", git("rev-parse", "--verify", "HEAD"))
    index = git("ls-files", "--stage", "-z")
    if any(entry.startswith(b"160000 ") for entry in index.split(b"\0")):
        raise SupervisorStateError("workspace snapshots with submodules are not supported")
    add(b"index", index)
    add(b"diff", git("-c", "core.quotePath=false", "diff", "--binary", "--no-ext-diff", "HEAD", "--"))
    untracked = sorted(path for path in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if path)
    for raw_path in untracked:
        path = Path(worktree_path) / os.fsdecode(raw_path)
        info = path.lstat()
        add(b"path", raw_path)
        add(b"mode", str(stat.S_IMODE(info.st_mode)).encode("ascii"))
        if stat.S_ISLNK(info.st_mode):
            add(b"link", os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as stream:
                file_hash = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            add(b"file", file_hash.digest())
        else:
            raise SupervisorStateError(f"unsupported untracked file: {os.fsdecode(raw_path)}")
    return "sha256:" + digest.hexdigest()

def _git_head(worktree_path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", worktree_path, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _git_diff_stat(worktree_path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", worktree_path, "diff", "--stat", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Executor adapters
# ---------------------------------------------------------------------------

class ClaudeExecutor:
    name = "claude"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.binary = str(getattr(settings, "conveyor_claude_bin", None) or os.getenv("CONVEYOR_CLAUDE_BIN", "claude")).strip() or "claude"
        # allow env override
        self.binary = os.getenv("CONVEYOR_CLAUDE_BIN", self.binary).strip() or self.binary

    def available(self) -> tuple[bool, str]:
        import shutil
        if shutil.which(self.binary) is None and not (os.path.sep in self.binary and Path(self.binary).is_file()):
            return False, f"claude binary not found: {self.binary}"
        return True, "ok"

    async def run(self, *, role: RoleConfig, prompt: str, worktree_path: str, timeout_seconds: int) -> ExecutionResult:
        started = _utc_now()
        ok, reason = self.available()
        if not ok:
            return ExecutionResult(success=False, error=reason, started_at=started, finished_at=_utc_now())
        tools = "Read,Glob,Grep" if "write" not in role.tools else "Read,Glob,Grep,Edit,Write,Bash"
        permission_mode = "plan" if "write" not in role.tools else "acceptEdits"
        cmd = [self.binary, "-p", "--output-format", "json", "--bare", "--strict-mcp-config",
               "--no-session-persistence", "--tools", tools, "--permission-mode", permission_mode]
        if role.model:
            cmd.extend(["--model", role.model])
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=worktree_path or None,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
            return ExecutionResult(success=False, error=f"timed out after {timeout_seconds}s", started_at=started,
                                  finished_at=_utc_now(), evidence={"stderr": (err or b"").decode("utf-8", "replace")[:2000]})
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.communicate()
            raise
        rc = proc.returncode
        stdout = (out or b"").decode("utf-8", "replace")
        stderr = (err or b"").decode("utf-8", "replace")
        # Parse JSON result if possible
        result_text = stdout
        reported_error = False
        try:
            obj = json.loads(stdout)
            if isinstance(obj, dict):
                result_text = str(obj.get("result") or obj.get("output") or stdout)
                reported_error = bool(obj.get("is_error"))
        except Exception:
            pass
        sha = _git_head(worktree_path) if worktree_path else ""
        success = rc == 0 and not reported_error
        return ExecutionResult(success=success, output=truncate(redact_text(result_text), 16000),
                              error="" if success else truncate(redact_text(stderr or result_text or f"exit {rc}"), 3000),
                              evidence={"return_code": rc, "stderr": truncate(redact_text(stderr), 2000),
                                        "diff_stat": _git_diff_stat(worktree_path) if worktree_path else ""},
                              started_at=started, finished_at=_utc_now(), sha=sha)

    async def cancel(self, execution_id: str) -> bool:
        return False


class CodexExecutor:
    name = "codex"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.binary = os.getenv("CODEX_BIN", getattr(settings, "codex_bin", "codex"))

    def available(self) -> tuple[bool, str]:
        return False, "codex executor adapter not fully implemented in this build"

    async def run(self, *, role: RoleConfig, prompt: str, worktree_path: str, timeout_seconds: int) -> ExecutionResult:
        started = _utc_now()
        return ExecutionResult(success=False, error="codex executor adapter not fully implemented in this build",
                               started_at=started, finished_at=_utc_now())

    async def cancel(self, execution_id: str) -> bool:
        return False


class AntigravityExecutor:
    name = "antigravity"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def available(self) -> tuple[bool, str]:
        return False, "antigravity executor is not implemented in this build"

    async def run(self, *, role: RoleConfig, prompt: str, worktree_path: str, timeout_seconds: int) -> ExecutionResult:
        started = _utc_now()
        return ExecutionResult(success=False, error="antigravity executor is not implemented in this build",
                              started_at=started, finished_at=_utc_now())

    async def cancel(self, execution_id: str) -> bool:
        return False


def get_executor(settings: Any, name: str) -> Executor:
    name = (name or "").strip().lower()
    if name == "claude":
        return ClaudeExecutor(settings)
    if name == "codex":
        return CodexExecutor(settings)
    if name == "antigravity":
        return AntigravityExecutor(settings)
    # Unknown executor: unavailable, no silent fallback
    class _Unknown:
        def available(self):
            return False, f"unknown executor: {name}"
        async def run(self, *, role, prompt, worktree_path, timeout_seconds):
            return ExecutionResult(success=False, error=f"unknown executor: {name}", started_at=_utc_now(), finished_at=_utc_now())
        async def cancel(self, execution_id):
            return False
    unknown = _Unknown()
    unknown.name = name or "unknown"
    return unknown


# ---------------------------------------------------------------------------
# Orchestration: analysis -> implement -> review -> (repair loop)
# ---------------------------------------------------------------------------

async def _run_validation(command: list[str], worktree_path: str, timeout: int) -> tuple[int, str]:
    """Run operator-provided argv without a shell; keep output bounded in memory."""
    with tempfile.TemporaryFile() as output:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=worktree_path, stdout=output, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, truncate(redact_text(str(exc)), 2000)
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return 124, f"timed out after {timeout}s"
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 12000))
        text = output.read().decode("utf-8", "replace")
        return proc.returncode, truncate(redact_text(text), 12000)


async def run_supervisor_flow(settings: Any, task_id: str, *, goal: str, worktree_path: str,
                              acceptance_criteria: str = "", max_repairs: int | None = None,
                              already_dispatched: bool = False,
                              validation_commands: list[list[str]] | None = None) -> dict[str, Any]:
    """Run the full supervisor loop, advancing state only from real execution results."""
    roles = load_roles(settings)
    timeout = task_timeout(settings)
    repairs = max_repairs if max_repairs is not None else max_repair_rounds(settings)
    # dispatch
    if not already_dispatched:
        dispatch_task(settings, task_id, worktree_path=worktree_path, base_sha=_git_head(worktree_path))
    transition(settings, task_id, "analyzing", stage="analysis")
    # Analyst
    analyst = roles["analyst"]
    analyst_executor = get_executor(settings, analyst.executor)
    ok, reason = analyst_executor.available()
    if not ok:
        transition(settings, task_id, "failed", stage="analysis", error=f"analyst executor unavailable: {reason}")
        release_worktree_lock(settings, worktree_path, task_id)
        return get_task(settings, task_id) or {}
    analysis_prompt = (
        f"You are the analyst. Goal: {goal}\n"
        f"Worktree: {worktree_path}\n"
        "Read the relevant code and constraints. Produce a concrete implementation plan, "
        "impact scope, and explicit acceptance criteria. Do not edit files."
    )
    if acceptance_criteria:
        analysis_prompt += f"\nOperator acceptance criteria: {acceptance_criteria}\n"
    a_result = await analyst_executor.run(role=analyst, prompt=analysis_prompt, worktree_path=worktree_path, timeout_seconds=timeout)
    if not a_result.success:
        transition(settings, task_id, "failed", stage="analysis", error=f"analysis failed: {a_result.error}")
        release_worktree_lock(settings, worktree_path, task_id)
        return get_task(settings, task_id) or {}
    plan = a_result.output
    record_handoff(settings, task_id, from_role="analyst", to_role="implementer", summary=plan,
                   sha=a_result.sha or _git_head(worktree_path), evidence_path="analysis")
    # Implementer + Reviewer loop
    round_no = 0
    repair_issues: list[str] = []
    while True:
        transition(settings, task_id, "implementing", stage="implementation")
        impl = roles["implementer"]
        impl_executor = get_executor(settings, impl.executor)
        ok, reason = impl_executor.available()
        if not ok:
            transition(settings, task_id, "failed", stage="implementation", error=f"implementer executor unavailable: {reason}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        impl_prompt = (
            f"You are the implementer (Antigravity-style). Goal: {goal}\n"
            f"Worktree: {worktree_path}\n"
            f"Analysis plan:\n{plan}\n"
            "Implement the changes, run appropriate checks. Return a change summary."
        )
        if repair_issues:
            impl_prompt += "\nPrevious validation or review issues to fix:\n" + "\n".join(f"- {i}" for i in repair_issues)
        i_result = await impl_executor.run(role=impl, prompt=impl_prompt, worktree_path=worktree_path, timeout_seconds=timeout)
        if not i_result.success:
            transition(settings, task_id, "failed", stage="implementation", error=f"implementation failed: {i_result.error}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        new_sha = _git_head(worktree_path)
        if not new_sha:
            transition(settings, task_id, "failed", stage="implementation", error="could not determine workspace HEAD")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        transition(settings, task_id, "implementing", stage="implementation", current_sha=new_sha)
        record_handoff(settings, task_id, from_role="implementer", to_role="reviewer",
                       summary=i_result.output, sha=new_sha, evidence_path="implementation")
        validation_summaries: list[str] = []
        if validation_commands:
            try:
                validation_snapshot = _workspace_snapshot(worktree_path)
            except (OSError, subprocess.SubprocessError, SupervisorStateError) as exc:
                transition(settings, task_id, "failed", stage="validation", error=f"cannot snapshot workspace: {exc}")
                release_worktree_lock(settings, worktree_path, task_id)
                return get_task(settings, task_id) or {}
            validation_issues = []
            transition(settings, task_id, "reviewing", stage="validation")
            for command in validation_commands:
                code, output = await _run_validation(command, worktree_path, timeout)
                if code == 0 and "unittest" in command and re.search(r"\bRan 0 tests?\b", output):
                    code = 1
                    output += "\nNo tests were discovered; validation cannot pass."
                result_summary = redact_text(f"Command: {json.dumps(command, ensure_ascii=False)}\nExit: {code}\n{output}")
                validation_summaries.append(truncate(result_summary, 3000))
                record_handoff(settings, task_id, from_role="validator", to_role="reviewer",
                               summary=result_summary, sha=new_sha, evidence_path="validation")
                if code != 0:
                    validation_issues.append(result_summary)
            if validation_issues:
                repair_issues = validation_issues
                if round_no >= repairs:
                    transition(settings, task_id, "failed", stage="repair_limit_exceeded",
                               error="validation failed after maximum repair rounds")
                    release_worktree_lock(settings, worktree_path, task_id)
                    return get_task(settings, task_id) or {}
                round_no += 1
                start_repair(settings, task_id, validation_issues)
                continue
            try:
                validation_unchanged = _workspace_snapshot(worktree_path) == validation_snapshot
            except (OSError, subprocess.SubprocessError, SupervisorStateError):
                validation_unchanged = False
            if not validation_unchanged:
                transition(settings, task_id, "failed", stage="validation", error="workspace changed during validation; results are invalid")
                release_worktree_lock(settings, worktree_path, task_id)
                return get_task(settings, task_id) or {}
        if _git_head(worktree_path) != new_sha:
            transition(settings, task_id, "failed", stage="validation", error="workspace HEAD changed during validation")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        # Reviewer
        transition(settings, task_id, "reviewing", stage="review")
        rev = roles["reviewer"]
        rev_executor = get_executor(settings, rev.executor)
        ok, reason = rev_executor.available()
        if not ok:
            transition(settings, task_id, "failed", stage="review", error=f"reviewer executor unavailable: {reason}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        try:
            review_snapshot = _workspace_snapshot(worktree_path)
        except (OSError, subprocess.SubprocessError, SupervisorStateError) as exc:
            transition(settings, task_id, "failed", stage="review", error=f"cannot snapshot workspace: {exc}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        try:
            task = get_task(settings, task_id) or {}
            base = task.get("base_sha") or new_sha
            diff_stat = subprocess.run(["git", "-C", worktree_path, "diff", "--stat", base, "--"],
                                       check=True, capture_output=True, text=True, timeout=30).stdout
            status = subprocess.run(["git", "-C", worktree_path, "status", "--short", "--untracked-files=all"],
                                    check=True, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            transition(settings, task_id, "failed", stage="review", error=f"cannot inspect changes: {exc}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        review_prompt = (
            f"You are the independent reviewer. Goal: {goal}\n"
            f"Worktree: {worktree_path}\n"
            f"Current SHA: {new_sha}\n"
            f"Base SHA: {base}\n"
            f"Diff stat:\n{diff_stat}\n"
            f"Workspace status (including untracked files):\n{status}\n"
            f"Analysis plan and criteria:\n{plan}\n"
            f"Implementation summary:\n{i_result.output}\n"
            f"Validation results:\n{chr(10).join(validation_summaries) or '(no commands supplied)'}\n"
            "Independently inspect the changed files and assess the acceptance criteria. "
            "Do not claim you ran commands unless you actually did. "
            "Reply with 'PASS' if acceptance criteria are met, otherwise 'FAIL: <issue1>; <issue2>'."
        )
        if acceptance_criteria:
            review_prompt += f"\nAcceptance criteria: {acceptance_criteria}\n"
        r_result = await rev_executor.run(role=rev, prompt=review_prompt, worktree_path=worktree_path, timeout_seconds=timeout)
        if not r_result.success:
            transition(settings, task_id, "failed", stage="review", error=f"review failed: {r_result.error}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        try:
            after_review = _workspace_snapshot(worktree_path)
        except (OSError, subprocess.SubprocessError, SupervisorStateError) as exc:
            transition(settings, task_id, "failed", stage="review", error=f"cannot snapshot workspace after review: {exc}")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        if review_snapshot != after_review:
            transition(settings, task_id, "failed", stage="review", error="workspace changed during review; acceptance is invalid")
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        review_text = r_result.output.strip()
        passed = bool(re.match(r"^PASS(?:\s|$|[:.!])", review_text, re.IGNORECASE))
        issues = []
        if not passed:
            # parse issues after FAIL:
            body = review_text
            if ":" in body:
                body = body.split(":", 1)[1]
            issues = [x.strip() for x in body.split(";") if x.strip()]
            if not issues:
                issues = [review_text]
        record_acceptance(settings, task_id, sha=new_sha, passed=passed, issues=issues,
                          evidence=review_text, snapshot=review_snapshot)
        if passed:
            transition(settings, task_id, "completed", stage="completed", current_sha=new_sha)
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        # failure -> repair loop
        round_no += 1
        if round_no > repairs:
            transition(settings, task_id, "failed", stage="repair_limit_exceeded",
                       error="max repair rounds exceeded; remaining issues reported")
            record_handoff(settings, task_id, from_role="reviewer", to_role="user",
                           summary="max repair rounds exceeded; remaining issues: " + "; ".join(issues),
                           sha=new_sha)
            release_worktree_lock(settings, worktree_path, task_id)
            return get_task(settings, task_id) or {}
        repair_issues = issues
        start_repair(settings, task_id, issues)
        # loop continues with implementer


def summary(settings: Any, task_id: str) -> str:
    task = get_task(settings, task_id)
    if task is None:
        return f"task not found: {task_id}"
    lines = [
        f"任务: {task['id']}",
        f"目标: {task['goal']}",
        f"状态: {task['state']} / 阶段: {task['stage']}",
        f"仓库: {task['repo_path']}",
        f"Worktree: {task['worktree_path'] or '(未分配)'}",
        f"分支: {task['branch'] or '(未分配)'}",
        f"代码版本: {task['current_sha'] or '(未知)'}",
        f"修复轮次: {task['repair_round']}/{task['max_repair_rounds']}",
    ]
    acc = effective_acceptance(settings, task_id)
    if acc is not None:
        lines.append(f"验收: {'通过' if acc['passed'] else '未通过'} @ {acc['sha']}")
    elif latest_acceptance(settings, task_id) is not None:
        lines.append("验收: 已失效（代码版本变化），需重新验证")
    if task.get("error"):
        lines.append(f"错误: {task['error']}")
    return "\n".join(lines)


# Back-compat aliases
SupervisorTask = dict
AntigravitySupervisor = None
AgentSupervisor = None
