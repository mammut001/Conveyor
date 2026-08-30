"""Cross-process execution ownership and command mailbox.

Conveyor intentionally remains a single-node, SQLite-backed control plane.
Telegram, Feishu and the Web Console can run as separate processes, so live
``CodexRunner`` objects must never be treated as shared state.  This module
provides a tiny coordination layer for the pieces that *must* cross a process
boundary without introducing Redis, a broker, or a second execution engine.

The queue remains authoritative for job state.  Runtime ownership is stored in
the queue job metadata and commands are persisted in the same SQLite database.
A process that actually owns a running job polls only commands addressed to its
owner id.  Currently the only cross-process command is ``cancel``.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redaction import redact_text, truncate

COMMAND_CANCEL = "cancel"
_COMMAND_KINDS = {COMMAND_CANCEL}
_DEFAULT_POLL_SECONDS = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_owner_id(role: str | None = None) -> str:
    explicit = str(os.getenv("CONVEYOR_RUNTIME_OWNER_ID", "")).strip()
    if explicit:
        prefix = explicit
    else:
        process_role = role or str(os.getenv("CONVEYOR_PROCESS_ROLE", "process")).strip() or "process"
        prefix = f"{process_role}:{socket.gethostname()}"
    return f"{prefix}:{os.getpid()}"


@dataclass(frozen=True)
class RuntimeCommand:
    id: str
    job_id: str
    owner_id: str
    kind: str
    status: str
    created_at: str
    claimed_at: str | None = None
    completed_at: str | None = None
    result: str | None = None


class RuntimeControl:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_commands (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        claimed_at TEXT,
                        completed_at TEXT,
                        result TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_runtime_commands_owner_status
                        ON runtime_commands(owner_id, status, created_at);
                    CREATE INDEX IF NOT EXISTS idx_runtime_commands_job
                        ON runtime_commands(job_id, created_at);
                    """
                )
        finally:
            conn.close()

    def bind_job_owner(self, job_id: str, owner_id: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT metadata_json FROM queued_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            try:
                metadata = json.loads(row[0] or "{}")
            except Exception:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["execution_owner_id"] = owner_id
            metadata["execution_owner_bound_at"] = _utc_now()
            conn.execute(
                "UPDATE queued_jobs SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), _utc_now(), job_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def owner_for_job(self, job_id: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT metadata_json FROM queued_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                metadata = json.loads(row[0] or "{}")
            except Exception:
                return None
            if not isinstance(metadata, dict):
                return None
            owner = str(metadata.get("execution_owner_id") or "").strip()
            return owner or None
        finally:
            conn.close()

    def submit(self, job_id: str, owner_id: str, kind: str) -> RuntimeCommand:
        if kind not in _COMMAND_KINDS:
            raise ValueError(f"unsupported runtime command: {kind}")
        if not job_id or not owner_id:
            raise ValueError("job_id and owner_id are required")
        command = RuntimeCommand(
            id=uuid.uuid4().hex,
            job_id=job_id,
            owner_id=owner_id,
            kind=kind,
            status="pending",
            created_at=_utc_now(),
        )
        conn = self._connect()
        try:
            with conn:
                existing = conn.execute(
                    """SELECT * FROM runtime_commands
                       WHERE job_id = ? AND owner_id = ? AND kind = ?
                         AND status IN ('pending', 'claimed')
                       ORDER BY created_at DESC LIMIT 1""",
                    (job_id, owner_id, kind),
                ).fetchone()
                if existing is not None:
                    return self._from_row(existing)
                conn.execute(
                    """INSERT INTO runtime_commands
                       (id, job_id, owner_id, kind, status, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?)""",
                    (command.id, command.job_id, command.owner_id, command.kind, command.created_at),
                )
            return command
        finally:
            conn.close()

    def claim_next(self, owner_id: str, job_id: str) -> RuntimeCommand | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM runtime_commands
                   WHERE owner_id = ? AND job_id = ? AND status = 'pending'
                   ORDER BY created_at ASC LIMIT 1""",
                (owner_id, job_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            claimed_at = _utc_now()
            updated = conn.execute(
                """UPDATE runtime_commands
                   SET status = 'claimed', claimed_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (claimed_at, row["id"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            values = dict(row)
            values["status"] = "claimed"
            values["claimed_at"] = claimed_at
            return RuntimeCommand(**values)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, command_id: str, result: str, *, failed: bool = False) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """UPDATE runtime_commands
                       SET status = ?, completed_at = ?, result = ?
                       WHERE id = ?""",
                    (
                        "failed" if failed else "completed",
                        _utc_now(),
                        truncate(redact_text(result or ""), 2_000),
                        command_id,
                    ),
                )
        finally:
            conn.close()

    def get(self, command_id: str) -> RuntimeCommand | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM runtime_commands WHERE id = ?",
                (command_id,),
            ).fetchone()
            return self._from_row(row) if row is not None else None
        finally:
            conn.close()

    def prune(self, max_age_seconds: int = 86_400) -> int:
        cutoff = time.time() - max(300, int(max_age_seconds))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, completed_at, created_at FROM runtime_commands WHERE status IN ('completed', 'failed')"
            ).fetchall()
            stale: list[str] = []
            for row in rows:
                raw = row["completed_at"] or row["created_at"]
                try:
                    ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if ts < cutoff:
                    stale.append(str(row["id"]))
            if not stale:
                return 0
            with conn:
                conn.executemany("DELETE FROM runtime_commands WHERE id = ?", [(item,) for item in stale])
            return len(stale)
        finally:
            conn.close()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RuntimeCommand:
        return RuntimeCommand(**{key: row[key] for key in row.keys()})


_controls: dict[str, RuntimeControl] = {}
_live_runner: Any | None = None
_live_owner_id: str | None = None
_live_command_task: asyncio.Task[None] | None = None
_live_job_id: str | None = None


def get_runtime_control(settings: Any) -> RuntimeControl:
    db_path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    key = str(db_path.resolve())
    control = _controls.get(key)
    if control is None:
        control = RuntimeControl(db_path)
        _controls[key] = control
    return control


def register_runtime_runner(runner: Any, *, role: str | None = None) -> str:
    global _live_runner, _live_owner_id
    _live_runner = runner
    if _live_owner_id is None:
        _live_owner_id = runtime_owner_id(role)
    return _live_owner_id


def bind_live_job(settings: Any, job_id: str) -> str | None:
    global _live_command_task, _live_job_id
    runner = _live_runner
    owner_id = _live_owner_id
    if runner is None or owner_id is None or not job_id:
        return None
    current = getattr(runner, "current_job", None)
    if current is None or str(getattr(current, "external_id", "")) != job_id:
        return None
    control = get_runtime_control(settings)
    control.bind_job_owner(job_id, owner_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return owner_id
    if _live_job_id == job_id and _live_command_task is not None and not _live_command_task.done():
        return owner_id
    if _live_command_task is not None and not _live_command_task.done():
        _live_command_task.cancel()
    _live_job_id = job_id
    _live_command_task = loop.create_task(
        watch_runtime_commands(settings, runner, job_id=job_id, owner_id=owner_id),
        name=f"conveyor-runtime-{job_id}",
    )
    return owner_id


async def watch_runtime_commands(
    settings: Any,
    runner: Any,
    *,
    job_id: str,
    owner_id: str,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
) -> None:
    control = get_runtime_control(settings)
    while True:
        current = getattr(runner, "current_job", None)
        if current is None or str(getattr(current, "external_id", "")) != job_id:
            return
        command = control.claim_next(owner_id, job_id)
        if command is None:
            await asyncio.sleep(max(0.2, float(poll_seconds)))
            continue
        if command.kind == COMMAND_CANCEL:
            try:
                result = await runner.cancel()
            except Exception as exc:
                control.complete(command.id, str(exc), failed=True)
            else:
                control.complete(command.id, str(result))
            return
        control.complete(command.id, "unsupported command", failed=True)
