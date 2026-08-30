"""Structured, SQLite-backed session transcript store.

This complements the legacy JSONL context cache in ``handlers/session.py``.
The JSONL file remains the prompt-injection source for backward compatibility;
this store is the durable UI/domain model for browser and future clients.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redaction import redact_text, truncate

_MAX_CONTENT = 16_000
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TranscriptMessage:
    id: str
    session_id: str
    role: str
    content: str
    created_at: str
    job_id: str | None = None
    kind: str | None = None
    metadata: dict[str, Any] | None = None


class TranscriptStore:
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
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        channel TEXT,
                        operator_id TEXT,
                        title TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_sessions_updated
                        ON sessions(archived, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS session_messages (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        job_id TEXT,
                        role TEXT NOT NULL,
                        kind TEXT,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        FOREIGN KEY(session_id) REFERENCES sessions(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_messages_session_time
                        ON session_messages(session_id, created_at, id);
                    CREATE INDEX IF NOT EXISTS idx_session_messages_job
                        ON session_messages(job_id, created_at);
                    """
                )
        finally:
            conn.close()

    def ensure_session(
        self,
        session_id: str,
        *,
        channel: str | None = None,
        operator_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise ValueError("session_id is required")
        now = _utc_now()
        safe_title = truncate(redact_text(title or "New session"), 120)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO sessions
                       (id, channel, operator_id, title, created_at, updated_at, archived)
                       VALUES (?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(id) DO UPDATE SET
                         channel = COALESCE(excluded.channel, sessions.channel),
                         operator_id = COALESCE(excluded.operator_id, sessions.operator_id),
                         updated_at = excluded.updated_at""",
                    (session_id, channel, operator_id, safe_title, now, now),
                )
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        job_id: str | None = None,
        kind: str | None = None,
        metadata: dict[str, Any] | None = None,
        channel: str | None = None,
        operator_id: str | None = None,
    ) -> TranscriptMessage:
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"unsupported transcript role: {role}")
        safe_content = truncate(redact_text(content or ""), _MAX_CONTENT)
        self.ensure_session(
            session_id,
            channel=channel,
            operator_id=operator_id,
            title=safe_content[:80] if role == "user" and safe_content else None,
        )
        message = TranscriptMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role=role,
            content=safe_content,
            created_at=_utc_now(),
            job_id=job_id,
            kind=kind,
            metadata=metadata or {},
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO session_messages
                       (id, session_id, job_id, role, kind, content, created_at, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        message.id,
                        message.session_id,
                        message.job_id,
                        message.role,
                        message.kind,
                        message.content,
                        message.created_at,
                        json.dumps(message.metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (message.created_at, session_id),
                )
            return message
        finally:
            conn.close()

    def list_sessions(self, limit: int = 50, *, include_archived: bool = False) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            where = "" if include_archived else "WHERE archived = 0"
            rows = conn.execute(
                f"""SELECT s.*,
                           (SELECT COUNT(*) FROM session_messages m WHERE m.session_id = s.id) AS message_count,
                           (SELECT COUNT(*) FROM queued_jobs q WHERE q.chat_id = s.id) AS job_count
                    FROM sessions s {where}
                    ORDER BY updated_at DESC LIMIT ?""",
                (min(max(1, int(limit)), 200),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["messages"] = [self._message_dict(item) for item in conn.execute(
                "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (session_id,),
            ).fetchall()]
            return value
        finally:
            conn.close()

    def messages(self, session_id: str, *, after: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            params: list[Any] = [session_id]
            sql = "SELECT * FROM session_messages WHERE session_id = ?"
            if after:
                sql += " AND created_at > ?"
                params.append(after)
            sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
            params.append(min(max(1, int(limit)), 2_000))
            return [self._message_dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def append_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        job_id: str | None = None,
        channel: str | None = None,
        operator_id: str | None = None,
        kind: str = "codex",
    ) -> tuple[TranscriptMessage, TranscriptMessage]:
        user = self.append(
            session_id,
            "user",
            user_text,
            job_id=job_id,
            kind=kind,
            channel=channel,
            operator_id=operator_id,
        )
        assistant = self.append(
            session_id,
            "assistant",
            assistant_text,
            job_id=job_id,
            kind=kind,
            channel=channel,
            operator_id=operator_id,
        )
        return user, assistant

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = {key: row[key] for key in row.keys() if key != "metadata_json"}
        try:
            value["metadata"] = json.loads(row["metadata_json"] or "{}")
        except Exception:
            value["metadata"] = {}
        return value


_stores: dict[str, TranscriptStore] = {}


def get_transcript_store(settings: Any) -> TranscriptStore:
    path = (Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3").resolve()
    key = str(path)
    store = _stores.get(key)
    if store is None:
        store = TranscriptStore(path)
        _stores[key] = store
    return store
