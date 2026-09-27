"""Shared domain services exposed by the authenticated Web Console."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_events import emit_event, get_event_store
from handlers.job_queue import JobQueue
from redaction import redact_text, truncate
from runtime_control import COMMAND_CANCEL, get_runtime_control
from transcript_store import get_transcript_store
from provider_config import get_provider_config, save_provider_config


class WebControl:
    def __init__(self, settings: Any, runner: Any, queue: JobQueue) -> None:
        self.settings = settings
        self.runner = runner
        self.queue = queue
        self.started_at = time.time()
        self._init_approvals()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.queue._db_path()), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _init_approvals(self) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS web_approvals (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        decided_at TEXT,
                        result TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_web_approvals_status
                        ON web_approvals(status, expires_at);
                    """
                )
        finally:
            connection.close()

    def list_jobs(self, limit: int = 100, session_id: str | None = None) -> list[dict[str, Any]]:
        jobs = self.queue.list_jobs(limit, session_id=session_id)
        latest = get_event_store(self.settings).latest_for_jobs(item["id"] for item in jobs)
        for item in jobs:
            event = latest.get(item["id"])
            item["latest_event"] = event.to_dict() if event else None
            item["changed_files"] = self._changed_files(item)
        return jobs

    def provider_config(self) -> dict[str, Any]:
        """Return the active provider configuration without secret material."""
        return get_provider_config(self.settings)

    def update_provider_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist the provider and its optional replacement key."""
        return save_provider_config(self.settings, payload)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        item = self.queue.job_snapshot(job_id)
        if item is None:
            return None
        events = get_event_store(self.settings).list(job_id, limit=2_000)
        item["latest_event"] = events[-1].to_dict() if events else None
        item["changed_files"] = self._changed_files(item)
        runtime = self._runtime_metadata(item)
        if runtime:
            item["runtime"] = runtime
        return item

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        transcript_sessions = get_transcript_store(self.settings).list_sessions(limit)
        jobs = self.queue.list_jobs(500)
        if transcript_sessions:
            latest_by_session: dict[tuple[str, str, str], dict[str, Any]] = {}
            for job in jobs:
                key = (
                    str(job.get("channel") or ""),
                    str(job.get("operator_id") or ""),
                    str(job.get("chat_id") or ""),
                )
                if all(key) and key not in latest_by_session:
                    latest_by_session[key] = job
            for session in transcript_sessions:
                session["last_activity"] = session.get("updated_at") or session.get("created_at")
                key = (
                    str(session.get("channel") or ""),
                    str(session.get("operator_id") or ""),
                    str(session.get("source_chat_id") or ""),
                )
                session["latest_job"] = latest_by_session.get(key)
            return transcript_sessions

        # Backward-compatible projection for installations that have not yet
        # written a structured transcript.
        grouped: dict[str, dict[str, Any]] = {}
        for job in jobs:
            session_id = str(job.get("chat_id") or "")
            if not session_id:
                continue
            session = grouped.setdefault(session_id, {
                "id": session_id,
                "channel": job.get("channel"),
                "created_at": job.get("created_at"),
                "last_activity": job.get("updated_at") or job.get("created_at"),
                "job_count": 0,
                "message_count": 0,
                "latest_job": None,
                "title": job.get("prompt_preview") or "Session",
            })
            session["job_count"] += 1
            if session["latest_job"] is None:
                session["latest_job"] = job
        return list(grouped.values())[:limit]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        transcript = get_transcript_store(self.settings).get_session(session_id)
        if transcript is not None:
            source_chat_id = str(transcript.get("source_chat_id") or "")
            jobs = self.list_jobs(200, session_id=source_chat_id)
            jobs = [job for job in jobs if (
                job.get("channel") == transcript.get("channel")
                and job.get("operator_id") == transcript.get("operator_id")
            )]
            transcript["last_activity"] = transcript.get("updated_at") or transcript.get("created_at")
            transcript["jobs"] = jobs
            transcript["job_count"] = len(jobs)
            return transcript
        jobs = self.list_jobs(200, session_id=session_id)
        if not jobs:
            return None
        return {
            "id": session_id,
            "channel": jobs[0].get("channel"),
            "created_at": jobs[-1].get("created_at"),
            "last_activity": jobs[0].get("updated_at") or jobs[0].get("created_at"),
            "jobs": jobs,
            "messages": [],
            "job_count": len(jobs),
        }

    def resolve_session_identity(self, session_id: str) -> tuple[str, str, str] | None:
        transcript = get_transcript_store(self.settings).get_session(session_id)
        if transcript is None:
            return None
        channel = str(transcript.get("channel") or "")
        operator_id = str(transcript.get("operator_id") or "")
        source_chat_id = str(transcript.get("source_chat_id") or "")
        if channel not in ("telegram", "feishu", "web") or not operator_id or not source_chat_id:
            return None
        return channel, operator_id, source_chat_id

    def events(self, job_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return [item.to_dict() for item in get_event_store(self.settings).list(job_id, after, limit)]

    def _runtime_metadata(self, job: dict[str, Any]) -> dict[str, Any] | None:
        runtime_id = str((job.get("metadata") or {}).get("runtime_job_id") or "")
        if not runtime_id or not all(c.isalnum() or c in "-_" for c in runtime_id):
            return None
        path = Path(self.settings.codex_task_root) / "logs" / runtime_id / "job.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(value, dict):
            return None
        allowed = {
            "id", "mode", "state", "started_at", "finished_at", "return_code",
            "worktree_path", "last_event", "attempt", "max_attempts", "usage",
        }
        return {key: value.get(key) for key in allowed if key in value}

    def _worktree(self, job: dict[str, Any]) -> Path | None:
        runtime = self._runtime_metadata(job) or {}
        raw = runtime.get("worktree_path") or (job.get("metadata") or {}).get("worktree_path")
        if not raw:
            return None
        path = Path(str(raw)).resolve()
        root = (Path(self.settings.codex_task_root) / "worktrees").resolve()
        if root not in path.parents:
            return None
        return path

    def _changed_files(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        worktree = self._worktree(job)
        if not worktree or not worktree.exists():
            return []
        import subprocess
        result = subprocess.run(
            ["git", "status", "--short"], cwd=worktree,
            capture_output=True, text=True, timeout=5, check=False,
        )
        files = []
        for line in result.stdout.splitlines()[:200]:
            if len(line) >= 4:
                files.append({"status": line[:2].strip(), "path": line[3:]})
        return files

    async def diff(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        worktree = self._worktree(job)
        text = await self.runner.diff_job(job_id, worktree)
        additions = deletions = 0
        files: list[dict[str, Any]] = []
        if worktree and worktree.exists():
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--numstat", "HEAD", "--", "."], cwd=worktree,
                capture_output=True, text=True, timeout=10, check=False,
            )
            for line in result.stdout.splitlines()[:500]:
                parts = line.split("\t", 2)
                if len(parts) != 3:
                    continue
                added, removed, path = parts
                add_n = int(added) if added.isdigit() else 0
                del_n = int(removed) if removed.isdigit() else 0
                additions += add_n; deletions += del_n
                files.append({"path": path, "additions": add_n, "deletions": del_n})
        return {
            "job_id": job_id, "worktree": str(worktree) if worktree else None,
            "diff": text, "stats": {"files": len(files), "additions": additions, "deletions": deletions},
            "files": files,
        }

    def request_approval(self, job_id: str, action: str) -> dict[str, Any]:
        if action not in ("apply", "discard"):
            raise ValueError("unsupported approval action")
        if self.get_job(job_id) is None:
            raise KeyError(job_id)
        approval_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO web_approvals
                       (id, job_id, action, status, created_at, expires_at)
                       VALUES (?, ?, ?, 'pending', ?, ?)""",
                    (approval_id, job_id, action, now, time.time() + 300),
                )
        finally:
            connection.close()
        event = emit_event(
            self.settings, "approval.required", job_id,
            {"approval_id": approval_id, "action": action, "expires_in_seconds": 300},
        )
        return {"id": approval_id, "job_id": job_id, "action": action, "status": "pending", "event_id": event.event_id}

    def list_approvals(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            now = time.time()
            with connection:
                connection.execute(
                    "UPDATE web_approvals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                    (now,),
                )
            rows = connection.execute(
                "SELECT * FROM web_approvals WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
            return [{key: row[key] for key in row.keys() if key != "result"} for row in rows]
        finally:
            connection.close()

    async def decide_approval(self, approval_id: str, approve: bool) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] != "pending" or float(row["expires_at"]) < time.time():
                connection.rollback()
                return {"id": approval_id, "status": "expired"}
            job_id = str(row["job_id"])
            action = str(row["action"])
            status = "accepted" if approve else "rejected"
            connection.execute(
                "UPDATE web_approvals SET status = ?, decided_at = ? WHERE id = ?",
                (status, datetime.now(timezone.utc).isoformat(), approval_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        emit_event(self.settings, f"approval.{status}", job_id, {
            "approval_id": approval_id, "action": action,
        })
        result = "Rejected by operator."
        if approve:
            job = self.get_job(job_id)
            if job is None:
                result = "Job no longer exists."
            else:
                worktree = self._worktree(job)
                emit_event(self.settings, f"{action}.started", job_id, {})
                if action == "apply":
                    result = await self.runner.apply_job(job_id, worktree)
                    kind = "apply.completed"
                else:
                    result = await self.runner.discard_job(job_id, worktree)
                    kind = "discard.completed"
                emit_event(self.settings, kind, job_id, {"result": result})
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "UPDATE web_approvals SET result = ? WHERE id = ?",
                    (truncate(redact_text(result), 4_000), approval_id),
                )
        finally:
            connection.close()
        return {"id": approval_id, "job_id": job_id, "action": action, "status": status, "result": result}

    async def cancel_job(self, job_id: str) -> tuple[bool, str]:
        job = self.get_job(job_id)
        if job is None:
            return False, "Job not found."
        if job.get("state") == "queued":
            return await self.queue.cancel(job_id)
        current = self.runner.current_job
        if current and getattr(current, "external_id", None) == job_id:
            result = await self.runner.cancel()
            return True, result
        if job.get("state") != "running":
            return False, f"Job is {job.get('state') or 'not running'}."
        control = get_runtime_control(self.settings)
        owner_id = str((job.get("metadata") or {}).get("execution_owner_id") or "").strip()
        if not owner_id:
            owner_id = control.owner_for_job(job_id) or ""
        if not owner_id:
            return False, "Running job has no live execution owner yet; retry after the runner starts streaming."
        command = control.submit(job_id, owner_id, COMMAND_CANCEL)
        emit_event(self.settings, "task.cancel_requested", job_id, {"command_id": command.id})
        return True, f"Cancellation requested for {job_id}."

    async def emergency_stop(self) -> str:
        from handlers.tools.executors import exec_computer_stop
        return await exec_computer_stop(self.settings, "")

    def computer_status(self) -> dict[str, Any]:
        from desktop_computer_requests import get_active_task, arm_remaining_seconds, is_direct_mode_active
        from desktop_upload_requests import list_recent_upload_requests
        screenshots: list[dict[str, Any]] = []
        for record in list_recent_upload_requests(self.settings, limit=5):
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            if record.get("status") != "completed" or not result.get("thumbnail_path"):
                continue
            screenshots.append({
                "artifact_id": record.get("upload_id"),
                "created_at": result.get("created_at") or record.get("updated_at"),
                "width": result.get("width"), "height": result.get("height"),
                "bytes": result.get("bytes"), "node_id": result.get("node_id"),
            })
        active = get_active_task(self.settings)
        return {
            "armed": is_direct_mode_active(self.settings),
            "arm_remaining_seconds": arm_remaining_seconds(self.settings),
            "active_task": active if isinstance(active, dict) else None,
            "screenshots": screenshots,
        }

    def artifact_path(self, artifact_id: str) -> Path | None:
        if not artifact_id or not all(ch.isalnum() or ch in "-_" for ch in artifact_id):
            return None
        from desktop_upload_requests import get_upload_request
        from handlers.tools.observe_tools import resolve_upload_temp_dir
        record = get_upload_request(self.settings, artifact_id)
        result = record.get("result") if isinstance(record, dict) and isinstance(record.get("result"), dict) else {}
        raw = result.get("thumbnail_path")
        if not raw:
            return None
        path = Path(str(raw)).resolve()
        root = resolve_upload_temp_dir(self.settings).resolve()
        if root not in path.parents or path.suffix.lower() != ".png" or not path.is_file():
            return None
        return path

    def nodes(self) -> list[dict[str, Any]]:
        from nodes.registry import list_nodes
        result = []
        for node in list_nodes(self.settings):
            result.append({
                "id": node.node_id,
                "name": node.display_name,
                "type": node.node_type.value,
                "status": node.status.value,
                "last_seen_at": node.last_seen_at,
                "capabilities": list(node.capabilities),
                "trust_level": node.trust_level.value,
                "metadata": node.metadata,
            })
        return result

    def system_status(self) -> dict[str, Any]:
        disk_path = Path(self.settings.codex_task_root)
        if not disk_path.exists():
            disk_path = Path(self.settings.codex_workspace_root)
        if not disk_path.exists():
            disk_path = Path.cwd()
        usage = shutil.disk_usage(disk_path)
        memory: dict[str, int | None] = {"total": None, "available": None}
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            values: dict[str, int] = {}
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, raw = line.partition(":")
                try:
                    values[key] = int(raw.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    continue
            memory = {"total": values.get("MemTotal"), "available": values.get("MemAvailable")}
        jobs = self.queue.list_jobs(500)
        counts: dict[str, int] = {}
        for job in jobs:
            state = str(job.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
        return {
            "uptime_seconds": int(time.time() - self.started_at),
            "load_average": list(os.getloadavg()),
            "cpu_count": os.cpu_count(),
            "memory": memory,
            "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
            "queue": {"depth": self.queue.queue_length, "paused": self.queue.is_paused, "states": counts},
            "channels": {
                "telegram": {"configured": bool(self.settings.telegram_bot_token)},
                "feishu": {"configured": bool(self.settings.lark_app_id and self.settings.lark_app_secret)},
            },
            "nodes": self.nodes(),
        }
