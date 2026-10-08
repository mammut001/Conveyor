"""Shared domain services exposed by the authenticated Web Console."""
from __future__ import annotations

import json
import re
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
        try:
            from handlers.tools.confirm import configure_confirmation_store
            configure_confirmation_store(self.queue._db_path())
        except Exception:
            pass

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
            return self._empty_agent_session(session_id)
        return {
            "id": session_id,
            "channel": jobs[0].get("channel"),
            "created_at": jobs[-1].get("created_at"),
            "last_activity": jobs[0].get("updated_at") or jobs[0].get("created_at"),
            "jobs": jobs,
            "messages": [],
            "job_count": len(jobs),
        }

    def _empty_agent_session(self, session_id: str) -> dict[str, Any] | None:
        """An agent's conversation exists from the moment the agent does."""
        import agents

        if not agents.enabled(self.settings):
            return None
        for agent in agents.AgentStore(self.settings).list():
            if agent["session_id"] == session_id:
                return {
                    "id": session_id, "channel": agents.WEB_CHANNEL, "operator_id": agents.WEB_OPERATOR,
                    "source_chat_id": agents.chat_id_for(agent["id"]), "title": agent["name"],
                    "created_at": None, "last_activity": None,
                    "jobs": [], "messages": [], "job_count": 0, "message_count": 0,
                }
        from worker_sessions import WorkerSessionStore

        row = WorkerSessionStore(self.settings).get(session_id)
        if row is None or row["archived"] or row["channel"] != agents.WEB_CHANNEL or row["kind"] == "legacy":
            return None
        agent = agents.AgentStore(self.settings).get(row["agent_id"])
        if agent is None or agent["archived"]:
            return None
        return {
            "id": session_id, "channel": row["channel"], "operator_id": row["operator_id"],
            "source_chat_id": row["source_chat_id"], "title": row["title"] or agent["name"],
            "created_at": None, "last_activity": None,
            "jobs": [], "messages": [], "job_count": 0, "message_count": 0,
        }

    def resolve_session_identity(self, session_id: str) -> tuple[str, str, str] | None:
        transcript = get_transcript_store(self.settings).get_session(session_id)
        if transcript is None:
            import agents
            from worker_sessions import WorkerSessionStore

            if not agents.enabled(self.settings):
                return None
            row = WorkerSessionStore(self.settings).get(session_id)
            if row is None or row["archived"] or row["kind"] == "legacy":
                return None
            agent = agents.AgentStore(self.settings).get(row["agent_id"])
            if agent is None or agent["archived"] or row["channel"] != agents.WEB_CHANNEL:
                return None
            return row["channel"], row["operator_id"], row["source_chat_id"]
        channel = str(transcript.get("channel") or "")
        operator_id = str(transcript.get("operator_id") or "")
        source_chat_id = str(transcript.get("source_chat_id") or "")
        if channel not in ("telegram", "feishu", "web") or not operator_id or not source_chat_id:
            return None
        return channel, operator_id, source_chat_id

    def archive_session(self, session_id: str) -> bool:
        from worker_sessions import WorkerSessionStore

        transcript = get_transcript_store(self.settings).archive_session(session_id)
        registry = WorkerSessionStore(self.settings).archive_registered(session_id)
        return transcript or registry

    def delete_session(self, session_id: str) -> bool:
        from worker_sessions import WorkerSessionStore

        transcript = get_transcript_store(self.settings).delete_session(session_id)
        # Keep the registry row, archived, so a deleted secondary cannot be
        # recreated as an empty guessed id.
        registry = WorkerSessionStore(self.settings).archive_registered(session_id)
        return transcript or registry

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
        approvals: list[dict[str, Any]] = []
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
            for row in rows:
                item = {key: row[key] for key in row.keys() if key != "result"}
                item["kind"] = "job"
                approvals.append(item)
        finally:
            connection.close()

        from handlers.tools.confirm import list_pending, _CONFIRM_TTL_SECONDS
        from handlers.tools.registry import get_tool
        from personal_tools.registry import get_personal_tool
        from transcript_store import session_identity

        try:
            import handlers.tools.executors  # register builtin tools
            from personal_tools.registry import register_personal_tools
            register_personal_tools()
        except Exception:
            pass

        for action in list_pending(channel="web", settings=self.settings):
            spec = get_tool(action.tool_name)
            if spec is not None:
                summary = spec.summary
            else:
                pspec = get_personal_tool(action.tool_name)
                summary = pspec.summary if pspec else action.tool_name

            approvals.append({
                "id": action.token,
                "kind": "tool",
                "tool_name": action.tool_name,
                "arg": action.arg,
                "summary": summary,
                "session_id": session_identity(action.channel, action.chat_id, action.operator_id),
                "status": "pending",
                "expires_at": action.expires_at,
            })
        return approvals

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
        # The job may be running on another lane's runner in this process.
        from job_lanes import runner_of_job
        lane_runner = runner_of_job(self.runner, job_id)
        if lane_runner is not None:
            result = await lane_runner.cancel()
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

    def request_host_screen(self) -> dict[str, Any]:
        """Request one explicit, read-only host screenshot for the Web Console."""
        if not getattr(self.settings, "conveyor_desktop_upload_enabled", False):
            return {
                "ok": False,
                "error": "thumbnail_preview_disabled",
                "message": "Host-screen thumbnail sharing is disabled; screenshots remain on the Mac.",
            }

        from channel.types import InboundMessage
        from desktop_observe_requests import create_observe_request, list_recent_observe_requests
        from desktop_upload_requests import list_recent_upload_requests

        marker = "web-console-host-screen-preview"
        uploads = list_recent_upload_requests(self.settings, limit=10)
        for record in list_recent_observe_requests(self.settings, limit=10):
            if record.get("created_by_channel") != "web" or record.get("user_request") != marker:
                continue
            upload = next(
                (item for item in uploads if item.get("observe_request_id") == record.get("request_id")),
                None,
            )
            pending_observe = record.get("status") in ("pending", "claimed")
            pending_upload = isinstance(upload, dict) and upload.get("status") in ("pending", "claimed")
            if pending_observe or pending_upload:
                return {
                    "ok": True,
                    "request": {
                        "request_id": record.get("request_id"),
                        "status": record.get("status"),
                        "created_at": record.get("created_at"),
                    },
                    "already_pending": True,
                }

        msg = InboundMessage(
            channel="web",
            operator_id="web-console",
            chat_id="web-console",
            message_id=f"web-screen-{uuid.uuid4().hex}",
            text="Capture host screen for Web Console preview",
            chat_type="p2p",
        )
        result = create_observe_request(
            self.settings,
            msg,
            marker,
            auto_upload_thumbnail=True,
            auto_delivery=False,
        )
        if not result.get("ok"):
            return result
        record = result.get("request") or {}
        return {
            "ok": True,
            "request": {
                "request_id": record.get("request_id"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
            },
        }

    def computer_status(self) -> dict[str, Any]:
        from desktop_computer_requests import get_active_task, arm_remaining_seconds, is_direct_mode_active
        from desktop_observe_requests import list_recent_observe_requests
        from desktop_upload_requests import (
            ensure_upload_request_for_observe,
            list_recent_upload_requests,
        )
        upload_records = list_recent_upload_requests(self.settings, limit=10)
        screen_request = None
        marker = "web-console-host-screen-preview"
        observe_records = list_recent_observe_requests(self.settings, limit=10)
        host_screen_records = [
            record for record in observe_records
            if record.get("created_by_channel") == "web" and record.get("user_request") == marker
        ]
        for record in host_screen_records:
            if (
                getattr(self.settings, "conveyor_desktop_upload_enabled", False)
                and record.get("status") == "completed"
                and record.get("auto_upload_thumbnail")
            ):
                ensure_upload_request_for_observe(
                    self.settings,
                    record,
                    created_by_channel="web",
                    created_by_chat_id="web-console",
                    created_by_operator_id="web-console",
                )
                upload_records = list_recent_upload_requests(self.settings, limit=10)
            upload = next(
                (item for item in upload_records if item.get("observe_request_id") == record.get("request_id")),
                None,
            )
            screen_request = {
                "request_id": record.get("request_id"),
                "status": record.get("status"),
                "created_at": record.get("created_at"),
                "error": record.get("error") if record.get("status") == "failed" else None,
                "upload_status": upload.get("status") if isinstance(upload, dict) else None,
            }
            break

        host_request_ids = {record.get("request_id") for record in host_screen_records}
        screenshots: list[dict[str, Any]] = []
        for record in upload_records:
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            if (
                record.get("observe_request_id") not in host_request_ids
                or record.get("status") != "completed"
                or not result.get("thumbnail_path")
            ):
                continue
            screenshots.append({
                "artifact_id": record.get("upload_id"),
                "created_at": result.get("created_at") or record.get("updated_at"),
                "width": result.get("width"), "height": result.get("height"),
                "bytes": result.get("bytes"), "node_id": result.get("node_id"),
            })
            if len(screenshots) == 5:
                break

        active = get_active_task(self.settings)
        return {
            "armed": is_direct_mode_active(self.settings),
            "arm_remaining_seconds": arm_remaining_seconds(self.settings),
            "active_task": active if isinstance(active, dict) else None,
            "screenshots": screenshots,
            "screen_preview_enabled": bool(
                getattr(self.settings, "conveyor_desktop_upload_enabled", False)
            ),
            "screen_request": screen_request,
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
            "features": {
                "long_term_memory": bool(getattr(self.settings, "long_term_memory_enabled", False)),
                "routines": bool(getattr(self.settings, "routines_enabled", False)),
                "webhooks": bool(getattr(self.settings, "webhooks_enabled", False))
                and bool(getattr(self.settings, "routines_enabled", False)),
                "approval_inbox": bool(getattr(self.settings, "approval_inbox_enabled", False)),
                "skills": bool(getattr(self.settings, "skills_enabled", False)),
                "provider_key_scoping": bool(getattr(self.settings, "child_env_scope_provider_keys", False)),
                "mobile_ui": bool(getattr(self.settings, "web_mobile_ui", False)),
                "mcp": bool(getattr(self.settings, "mcp_enabled", False)),
                "approval_relay": bool(getattr(self.settings, "approval_relay_enabled", False)),
                "subagents": bool(getattr(self.settings, "subagents_enabled", False))
                and bool(getattr(self.settings, "chat_tools_enabled", False)),
                "teammate": bool(getattr(self.settings, "teammate_enabled", True)),
                "live_screen": bool(getattr(self.settings, "live_screen_enabled", False)),
                "agents": getattr(self.settings, "agents_enabled", False) is True,
            },
        }

    # ---- agents -----------------------------------------------------------

    def _agent_view(
        self,
        agent: dict[str, Any],
        sessions: dict[str, dict[str, Any]],
        waiting: set[str],
        jobs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """An agent plus what its row in the list shows: preview and status."""
        from redaction import redact_text
        from worker_sessions import WorkerSessionStore

        canonical = WorkerSessionStore(self.settings).list(agent["id"]) if not agent.get("archived") else []
        transcript = get_transcript_store(self.settings)
        last: dict[str, Any] | None = None
        status = "idle"
        owned_ids = {item["session_id"] for item in canonical}
        owned_chats = {
            (item["channel"], item["operator_id"], item["source_chat_id"]) for item in canonical
        }
        for item in canonical:
            message = transcript.last_message(item["session_id"])
            if message and (last is None or str(message.get("created_at") or "") >= str(last.get("created_at") or "")):
                last = message
            if item["session_id"] in waiting:
                status = "waiting"
        active_states = ("running", "queued")
        for job in jobs or []:
            key = (str(job.get("channel") or ""), str(job.get("operator_id") or ""), str(job.get("chat_id") or ""))
            if key not in owned_chats and str(job.get("session_id") or "") not in owned_ids:
                continue
            if str(job.get("session_id") or "") in waiting or job.get("state") in ("needs_approval", "approval"):
                status = "waiting"
            elif status != "waiting" and job.get("state") in active_states:
                status = "working"
        # A newer terminal job must not hide an older running job, including
        # one past the recent-job snapshot. Confirmations live in the shared
        # file so another process's pending tool still shows as waiting.
        try:
            from handlers.tools.confirm import shared_pending_contexts
            pending = shared_pending_contexts(self.queue._db_path())
            if owned_chats & pending:
                status = "waiting"
        except Exception:
            pass
        try:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT channel, operator_id, chat_id, state FROM queued_jobs WHERE state IN ('queued', 'running')"
                ).fetchall()
            finally:
                connection.close()
            for row in rows:
                key = (str(row["channel"] or ""), str(row["operator_id"] or ""), str(row["chat_id"] or ""))
                if key in owned_chats and status != "waiting":
                    status = "working"
        except Exception:
            pass
        session = sessions.get(agent["session_id"]) or {}
        preview = " ".join(redact_text(str((last or {}).get("content") or "")).split())[:160]
        # Jobs only run in a project folder that is the root of a git repository.
        workspace_status = ""
        if agent.get("workspace_path"):
            folder = Path(agent["workspace_path"])
            workspace_status = "ok" if (folder / ".git").exists() else ("not_git" if folder.is_dir() else "missing")
        return {
            **agent,
            "workspace_status": workspace_status,
            "status": status,
            "last_message": preview,
            "last_message_role": (last or {}).get("role"),
            "last_activity": (last or {}).get("created_at") or session.get("last_activity"),
            "message_count": session.get("message_count") or 0,
            "sessions": [
                {
                    "id": item["session_id"],
                    "title": item["title"],
                    "kind": item["kind"],
                    "source_chat_id": item["source_chat_id"],
                    "channel": item["channel"],
                    "operator_id": item["operator_id"],
                }
                for item in canonical
            ],
        }

    def list_agents(self) -> dict[str, Any]:
        import agents

        if not agents.enabled(self.settings):
            return {"enabled": False, "agents": []}
        sessions = {str(item.get("id")): item for item in self.list_sessions(200)}
        waiting = {str(item.get("session_id") or "") for item in self.list_approvals()}
        jobs = self.list_jobs(200)
        return {
            "enabled": True,
            "agents": [
                self._agent_view(agent, sessions, waiting, jobs)
                for agent in agents.AgentStore(self.settings).list()
            ],
        }

    def save_agent(self, agent_id: str | None, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Create (agent_id None) or update an agent; None when it does not exist."""
        import agents

        store = agents.AgentStore(self.settings)
        agent = store.create(payload) if agent_id is None else store.update(agent_id, payload)
        return self._agent_view(agent, {}, set()) if agent else None

    def list_agent_sessions(self, agent_id: str) -> dict[str, Any] | None:
        import agents
        from worker_sessions import WorkerSessionStore

        if not agents.enabled(self.settings):
            return None
        agent = agents.AgentStore(self.settings).get(agent_id)
        if agent is None or agent["archived"]:
            return None
        rows = WorkerSessionStore(self.settings).list(agent_id)
        return {"agent_id": agent_id, "sessions": rows}

    def create_agent_session(self, agent_id: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        import agents
        from worker_sessions import WorkerSessionStore

        if not agents.enabled(self.settings):
            return None
        agent = agents.AgentStore(self.settings).get(agent_id)
        if agent is None or agent["archived"]:
            return None
        title = (payload or {}).get("title")
        return WorkerSessionStore(self.settings).create(agent_id, title)

    def archive_agent(self, agent_id: str) -> bool:
        import agents

        return agents.AgentStore(self.settings).archive(agent_id)

    def _agent_screenshot_node(self, agent: dict[str, Any]) -> str:
        """Node name recorded on screenshots taken on this agent's desktop."""
        import agents
        from desktop_computer_requests import x11_node_id

        if agent.get("display") is not None and agents.desktops_enabled(self.settings):
            return x11_node_id(agents.takeover_scope(agent["id"]))
        return str(getattr(self.settings, "conveyor_desktop_node_id", "") or "")

    def _agent_screenshots(self, agent: dict[str, Any], limit: int = 12) -> list[dict[str, Any]]:
        from desktop_screenshot import resolve_screenshot_dir

        node = self._agent_screenshot_node(agent)
        root = resolve_screenshot_dir(self.settings)
        if not node or not root.is_dir():
            return []
        found: list[dict[str, Any]] = []
        metas = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:400]
        for meta in metas:
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("node_id") != node or not meta.with_suffix(".png").is_file():
                continue
            found.append({
                "id": meta.stem, "created_at": data.get("created_at"),
                "width": data.get("width"), "height": data.get("height"),
            })
            if len(found) >= limit:
                break
        return found

    def agent_screenshot_path(self, agent_id: str, screenshot_id: str) -> Path | None:
        """PNG of one screenshot, only if it was taken on that agent's desktop."""
        import agents
        from desktop_screenshot import resolve_screenshot_dir

        if not agents.enabled(self.settings) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}", screenshot_id or ""):
            return None
        agent = agents.AgentStore(self.settings).get(agent_id)
        if agent is None or agent["archived"]:
            return None
        root = resolve_screenshot_dir(self.settings).resolve()
        png = (root / f"{screenshot_id}.png").resolve()
        meta = png.with_suffix(".json")
        if root not in png.parents or not png.is_file() or not meta.is_file():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        node = self._agent_screenshot_node(agent)
        return png if node and isinstance(data, dict) and data.get("node_id") == node else None

    def agent_library(self, agent_id: str) -> dict[str, Any] | None:
        """What an agent has accumulated: files it changed, its checks, memory, screenshots."""
        import agents

        if not agents.enabled(self.settings):
            return None
        agent = agents.AgentStore(self.settings).get(agent_id)
        if agent is None or agent["archived"]:
            return None
        session = self.get_session(agent["session_id"]) or {}
        jobs = [
            {
                "id": job.get("id"), "state": job.get("state"),
                "prompt_preview": job.get("prompt_preview"), "updated_at": job.get("updated_at"),
                "changed_files": job.get("changed_files"),
            }
            for job in (session.get("jobs") or [])[:30] if job.get("changed_files")
        ]
        routine_items: list[dict[str, Any]] = []
        if getattr(self.settings, "routines_enabled", False):
            import routines
            routine_items = [
                {key: item.get(key) for key in ("id", "name", "schedule", "enabled", "next_run_at", "last_run_at")}
                for item in routines.list_routines(self.settings)
                if item.get("agent_id") == agent_id
            ]
        memory = None
        if getattr(self.settings, "long_term_memory_enabled", False):
            from personal_tools import long_term_memory as ltm
            owner = ltm.WEB_OPERATOR if agent["is_default"] else ltm.agent_owner(agent_id)
            rows = ltm.list_facts(self.settings, owner)
            memory = {
                "profile": sum(1 for row in rows if row["kind"] == "profile"),
                "log": sum(1 for row in rows if row["kind"] == "log"),
            }
        return {
            "agent_id": agent_id, "jobs": jobs, "routines": routine_items, "memory": memory,
            "screenshots": self._agent_screenshots(agent),
        }

    def teammate_status(self) -> dict[str, Any]:
        """Return structured Always-On Teammate sentry telemetry."""
        from personal_tools.sentry import SentryState, teammate_status_text
        state = SentryState.load(self.settings)
        return {
            "enabled": bool(getattr(self.settings, "teammate_enabled", True)),
            "is_paused": state.is_effective_paused(),
            "paused_until": state.paused_until,
            "muted_sources": state.muted_sources,
            "interval_seconds": getattr(self.settings, "sentry_interval_seconds", 300),
            "cooldown_seconds": getattr(self.settings, "sentry_cooldown_seconds", 7200),
            "last_patrol_at": state.last_patrol_at,
            "total_alerts_count": state.total_alerts_count,
            "recent_alerts": state.recent_alerts,
            "monitored_services": list(getattr(self.settings, "sentry_monitored_services", ())),
            "thresholds": {
                "disk_pct": getattr(self.settings, "sentry_disk_threshold_pct", 90.0),
                "disk_gb": getattr(self.settings, "sentry_disk_threshold_gb", 3.0),
                "load_ratio": getattr(self.settings, "sentry_load_threshold_ratio", 2.0),
                "error_burst": getattr(self.settings, "sentry_error_burst_threshold", 5),
            },
            "status_text": teammate_status_text(self.settings),
        }

    def teammate_run_patrol(self, force: bool = True) -> dict[str, Any]:
        """Trigger an immediate live sentry health inspection."""
        from personal_tools.sentry import run_sentry_patrol
        from dataclasses import asdict
        to_deliver, suppressed = run_sentry_patrol(self.settings, force=force, dry_run=True)
        return {
            "ok": True,
            "is_healthy": len(to_deliver) == 0,
            "alerts": [asdict(a) for a in to_deliver],
            "suppressed": [asdict(a) for a in suppressed],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def teammate_action(self, action: str, value: Any = None) -> dict[str, Any]:
        """Execute a state mutation action on the sentry (pause, resume, mute, unmute)."""
        from personal_tools.sentry import SentryState
        state = SentryState.load(self.settings)
        if action == "pause":
            try:
                hours = float(value) if value else 24.0
            except (ValueError, TypeError):
                hours = 24.0
            until = state.pause(hours)
            state.save(self.settings)
            return {"ok": True, "action": "pause", "paused_until": until}
        elif action == "resume":
            state.resume()
            state.save(self.settings)
            return {"ok": True, "action": "resume"}
        elif action == "mute":
            source = str(value or "").strip().lower()
            changed = state.mute(source)
            if changed:
                state.save(self.settings)
            return {"ok": True, "action": "mute", "muted_sources": state.muted_sources}
        elif action == "unmute":
            source = str(value or "").strip().lower()
            changed = state.unmute(source)
            if changed:
                state.save(self.settings)
            return {"ok": True, "action": "unmute", "muted_sources": state.muted_sources}
        return {"ok": False, "error": f"Unknown action '{action}'"}

