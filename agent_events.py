"""Stable, persisted agent/job events for chat and browser clients.

The queue database is deliberately reused: Conveyor remains a single-node
control plane and does not need a broker.  Event payloads are small JSON
objects; screenshots and other large artifacts are referenced by identifier.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from redaction import redact_text
from security.secrets import is_sensitive_key

SCHEMA_VERSION = 1
DEFAULT_RETENTION_PER_JOB = 2_000
MAX_PAYLOAD_TEXT = 16_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)[:MAX_PAYLOAD_TEXT]
    if isinstance(value, dict):
        return {
            str(key)[:128]: ("[REDACTED]" if is_sensitive_key(str(key)) else _safe_value(item, depth + 1))
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value[:100]]
    return redact_text(str(value))[:MAX_PAYLOAD_TEXT]


@dataclass(frozen=True)
class AgentEvent:
    schema_version: int
    event_id: str
    sequence: int
    timestamp: str
    kind: str
    job_id: str
    payload: dict[str, Any]
    session_id: str | None = None
    tool_call_id: str | None = None
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentEvent":
        return cls(
            schema_version=int(value["schema_version"]),
            event_id=str(value["event_id"]),
            sequence=int(value["sequence"]),
            timestamp=str(value["timestamp"]),
            kind=str(value["kind"]),
            job_id=str(value["job_id"]),
            payload=dict(value.get("payload") or {}),
            session_id=value.get("session_id"),
            tool_call_id=value.get("tool_call_id"),
            correlation_id=value.get("correlation_id"),
        )


class EventStore:
    """SQLite event log with deterministic per-job sequence numbers."""

    def __init__(self, db_path: Path, retention_per_job: int = DEFAULT_RETENTION_PER_JOB) -> None:
        self.db_path = Path(db_path)
        self.retention_per_job = max(100, int(retention_per_job))
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        self._initialize(connection)
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_events'"
            ).fetchone()
            if exists:
                return
        with self._init_lock:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_events (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    session_id TEXT,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    tool_call_id TEXT,
                    correlation_id TEXT,
                    payload_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    UNIQUE(job_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_job_sequence
                    ON agent_events(job_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_agent_events_session_time
                    ON agent_events(session_id, timestamp);
                """
            )
            connection.commit()
            self._initialized = True

    def append(
        self,
        kind: str,
        job_id: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        tool_call_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AgentEvent:
        if not kind or not job_id:
            raise ValueError("kind and job_id are required")
        safe_payload = _safe_value(payload or {})
        assert isinstance(safe_payload, dict)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            sequence = int(row[0])
            event = AgentEvent(
                schema_version=SCHEMA_VERSION,
                event_id=uuid.uuid4().hex,
                sequence=sequence,
                timestamp=_utc_now(),
                kind=kind,
                job_id=job_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                correlation_id=correlation_id,
                payload=safe_payload,
            )
            connection.execute(
                """INSERT INTO agent_events
                   (event_id, job_id, session_id, sequence, timestamp, kind,
                    tool_call_id, correlation_id, payload_json, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id, event.job_id, event.session_id, event.sequence,
                    event.timestamp, event.kind, event.tool_call_id,
                    event.correlation_id,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                    event.schema_version,
                ),
            )
            cutoff = sequence - self.retention_per_job
            if cutoff > 0:
                connection.execute(
                    "DELETE FROM agent_events WHERE job_id = ? AND sequence <= ?",
                    (job_id, cutoff),
                )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(self, job_id: str, after_sequence: int = 0, limit: int = 500) -> list[AgentEvent]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM agent_events
                   WHERE job_id = ? AND sequence > ?
                   ORDER BY sequence ASC LIMIT ?""",
                (job_id, max(0, int(after_sequence)), min(max(1, int(limit)), 2_000)),
            ).fetchall()
            return [self._from_row(row) for row in rows]
        finally:
            connection.close()

    def list_after_event(self, job_id: str, event_id: str | None, limit: int = 500) -> list[AgentEvent]:
        if not event_id:
            return self.list(job_id, limit=limit)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT sequence FROM agent_events WHERE job_id = ? AND event_id = ?",
                (job_id, event_id),
            ).fetchone()
        finally:
            connection.close()
        return self.list(job_id, int(row[0]) if row else 0, limit)

    def latest_for_jobs(self, job_ids: Iterable[str]) -> dict[str, AgentEvent]:
        ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
        result: dict[str, AgentEvent] = {}
        # Stay below SQLite's conservative host-parameter limit while avoiding
        # one query (and up to the full retention window) per dashboard row.
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            connection = self._connect()
            try:
                rows = connection.execute(
                    f"""SELECT event.*
                        FROM agent_events AS event
                        JOIN (
                            SELECT job_id, MAX(sequence) AS sequence
                            FROM agent_events
                            WHERE job_id IN ({placeholders})
                            GROUP BY job_id
                        ) AS latest
                        ON event.job_id = latest.job_id
                        AND event.sequence = latest.sequence""",
                    chunk,
                ).fetchall()
                for row in rows:
                    event = self._from_row(row)
                    result[event.job_id] = event
            finally:
                connection.close()
        return result

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AgentEvent:
        payload = json.loads(row["payload_json"])
        return AgentEvent(
            schema_version=int(row["schema_version"]),
            event_id=str(row["event_id"]),
            sequence=int(row["sequence"]),
            timestamp=str(row["timestamp"]),
            kind=str(row["kind"]),
            job_id=str(row["job_id"]),
            session_id=row["session_id"],
            tool_call_id=row["tool_call_id"],
            correlation_id=row["correlation_id"],
            payload=payload,
        )


_stores: dict[str, EventStore] = {}
_stores_lock = threading.Lock()


def event_db_path(settings: Any) -> Path:
    return Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"


def get_event_store(settings: Any) -> EventStore:
    path = str(event_db_path(settings).resolve())
    with _stores_lock:
        if path not in _stores:
            retention = getattr(settings, "conveyor_event_retention_per_job", DEFAULT_RETENTION_PER_JOB)
            _stores[path] = EventStore(Path(path), retention_per_job=retention)
        return _stores[path]


def emit_event(settings: Any, kind: str, job_id: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> AgentEvent:
    return get_event_store(settings).append(kind, job_id, payload, **kwargs)


def emit_codex_event(settings: Any, job_id: str, raw: dict[str, Any]) -> AgentEvent | None:
    """Translate a Codex JSONL envelope into the stable public vocabulary.

    Only small display-safe fields cross the boundary; raw environment data,
    full command payloads and reasoning are intentionally not persisted here.
    """
    event_type = str(raw.get("type") or raw.get("event") or "").lower()
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    item_type = str(item.get("type") or "").lower()
    if "reasoning" in event_type or "reasoning" in item_type:
        return None

    toolish = any(tag in item_type for tag in ("function_call", "tool_call", "command_execution"))
    tool_call_id = str(item.get("id") or item.get("call_id") or "") or None
    payload: dict[str, Any] = {
        "source_type": event_type,
    }
    if toolish:
        name = item.get("name") or item.get("tool") or item.get("function")
        if not name and "command_execution" in item_type:
            name = "shell"
        payload["name"] = str(name or "tool")[:128]
        payload["status"] = str(item.get("status") or "")[:64]
        for key in ("output", "text", "error"):
            if isinstance(item.get(key), str):
                payload[key] = item[key][:MAX_PAYLOAD_TEXT]
        if event_type in ("item.started", "item.updated"):
            kind = "tool.started" if event_type == "item.started" else "tool.output"
        elif event_type == "item.completed":
            status = str(item.get("status") or "").lower()
            kind = "tool.failed" if status in ("failed", "error") else "tool.completed"
        else:
            kind = "tool.output"
        return emit_event(settings, kind, job_id, payload, tool_call_id=tool_call_id)

    text_value = None
    if "agent_message" in item_type and isinstance(item.get("text"), str):
        text_value = item.get("text")
    if text_value is None:
        for key in ("delta", "text", "message", "summary"):
            if isinstance(raw.get(key), str) and raw[key].strip():
                text_value = raw[key]
                break
    if text_value:
        kind = "assistant.completed" if event_type == "item.completed" else "assistant.delta"
        return emit_event(settings, kind, job_id, {"text": text_value})
    return None
