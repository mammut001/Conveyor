"""Owned agent sessions, physical-context selection, and callback tokens.

Shares ``state/job_queue.sqlite3`` with ``AgentStore``. Rows are additive:
primary web conversations stay ``agents.session_id_for`` even before a row
exists. Clients cannot assign an arbitrary session id or another operator's chat.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from agents import AgentError, AgentStore, WEB_CHANNEL, WEB_OPERATOR, session_id_for
from transcript_store import session_identity

_KINDS = ("main", "created", "legacy")
_SECONDARY_RE = re.compile(r"^agent-[a-z0-9]{1,32}-s-[0-9a-f]{12}$")
_ACTION_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_MAX_TITLE = 120
_MAX_EXTRA = 500
_DEFAULT_TTL = 900.0

_initialised: set[str] = set()


def _clean_title(value: Any, fallback: str) -> str:
    title = " ".join(str(value or "").split())[:_MAX_TITLE]
    return title or fallback


class WorkerSessionStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        root = Path(settings.codex_memory_root) / "state"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "job_queue.sqlite3"
        key = str(self.path)
        if key not in _initialised or not self.path.exists():
            self._init_db()
            _initialised.add(key)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS worker_sessions (
                        session_id TEXT PRIMARY KEY,
                        agent_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        operator_id TEXT NOT NULL,
                        source_chat_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_sessions_identity
                        ON worker_sessions(channel, operator_id, source_chat_id);
                    CREATE TABLE IF NOT EXISTS worker_selections (
                        channel TEXT NOT NULL,
                        source_chat_id TEXT NOT NULL,
                        operator_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (channel, source_chat_id, operator_id)
                    );
                    CREATE TABLE IF NOT EXISTS worker_callback_tokens (
                        token TEXT PRIMARY KEY,
                        operator_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        topic TEXT NOT NULL DEFAULT '',
                        agent_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        page INTEGER NOT NULL DEFAULT 0,
                        extra_json TEXT NOT NULL DEFAULT '{}',
                        expires_at REAL NOT NULL
                    );
                    """
                )
        finally:
            conn.close()

    def _active_agent(self, agent_id: str) -> dict[str, Any]:
        import agents

        if not agents.enabled(self.settings):
            raise AgentError("agents are disabled")
        agent = AgentStore(self.settings).get(agent_id)
        if agent is None or agent["archived"]:
            raise AgentError("agent is not available")
        return agent

    @staticmethod
    def is_secondary_chat_id(chat_id: str) -> bool:
        return _SECONDARY_RE.fullmatch(str(chat_id or "")) is not None

    def _row_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["archived"] = bool(item["archived"])
        return item

    def _synthetic_main(self, agent: dict[str, Any]) -> dict[str, Any]:
        chat_id = f"agent-{agent['id']}"
        return {
            "session_id": session_id_for(agent["id"]),
            "agent_id": agent["id"],
            "channel": WEB_CHANNEL,
            "operator_id": WEB_OPERATOR,
            "source_chat_id": chat_id,
            "title": agent["name"],
            "kind": "main",
            "archived": bool(agent["archived"]),
            "created_at": agent.get("created_at"),
        }

    def get(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM worker_sessions WHERE session_id = ?", (str(session_id or ""),)
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return self._row_dict(row)
        for agent in AgentStore(self.settings).list(include_archived=True):
            if session_id_for(agent["id"]) == session_id:
                return self._synthetic_main(agent)
        return None

    def owner_agent_id(self, channel: str, source_chat_id: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT agent_id FROM worker_sessions
                   WHERE channel = ? AND source_chat_id = ? AND archived = 0""",
                (channel, str(source_chat_id or "")),
            ).fetchone()
        finally:
            conn.close()
        return str(row["agent_id"]) if row else None

    def list(self, agent_id: str) -> list[dict[str, Any]]:
        """Canonical web sessions for one agent. Legacy IM rows stay off this list."""
        agent = AgentStore(self.settings).get(agent_id)
        if agent is None:
            raise AgentError("agent is not available")
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM worker_sessions
                   WHERE agent_id = ? AND channel = ? AND kind IN ('main', 'created') AND archived = 0
                   ORDER BY created_at ASC""",
                (agent_id, WEB_CHANNEL),
            ).fetchall()
        finally:
            conn.close()
        found = [self._row_dict(row) for row in rows]
        if not any(item["kind"] == "main" for item in found) and not agent["archived"]:
            found.insert(0, self._synthetic_main(agent))
        return found

    def create(self, agent_id: str, title: Any = None) -> dict[str, Any]:
        agent = self._active_agent(agent_id)
        label = _clean_title(title, agent["name"])
        conn = self._connect()
        try:
            for _ in range(5):
                source = f"agent-{agent_id}-s-{uuid.uuid4().hex[:12]}"
                session_id = session_identity(WEB_CHANNEL, source, WEB_OPERATOR)
                now = time.time()
                try:
                    with conn:
                        conn.execute(
                            """INSERT INTO worker_sessions
                               (session_id, agent_id, channel, operator_id, source_chat_id, title, kind, archived, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, 'created', 0, ?)""",
                            (session_id, agent_id, WEB_CHANNEL, WEB_OPERATOR, source, label, now),
                        )
                except sqlite3.IntegrityError:
                    continue
                row = conn.execute("SELECT * FROM worker_sessions WHERE session_id = ?", (session_id,)).fetchone()
                return self._row_dict(row)
        finally:
            conn.close()
        raise AgentError("could not allocate a session")

    def register_legacy(
        self,
        agent_id: str,
        *,
        channel: str,
        operator_id: str,
        source_chat_id: str,
        requester_operator: str,
        current_source: str,
        title: Any = None,
    ) -> dict[str, Any]:
        """Bind an existing IM chat to an agent for this operator's current chat only."""
        self._active_agent(agent_id)
        channel = str(channel or "")
        if str(operator_id) != str(requester_operator):
            raise AgentError("session is outside this operator")
        if channel == "telegram":
            from channel.telegram_identity import TelegramAddress

            try:
                requested = TelegramAddress.parse(source_chat_id)
                current = TelegramAddress.parse(current_source)
            except ValueError as exc:
                raise AgentError("invalid telegram chat") from exc
            if requested.source != current.source:
                raise AgentError("session is outside this chat")
            source = requested.source
        elif channel == "feishu":
            source = str(source_chat_id or "")
            if not source or source != str(current_source or ""):
                raise AgentError("session is outside this chat")
        else:
            raise AgentError("legacy sessions are only for telegram or feishu")
        session_id = session_identity(channel, source, str(operator_id))
        existing = self.get(session_id)
        if existing is not None:
            if existing["agent_id"] != agent_id or existing["kind"] == "main":
                raise AgentError("session is already registered")
            return existing
        label = _clean_title(title, source)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO worker_sessions
                       (session_id, agent_id, channel, operator_id, source_chat_id, title, kind, archived, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'legacy', 0, ?)""",
                    (session_id, agent_id, channel, str(operator_id), source, label, time.time()),
                )
            row = conn.execute("SELECT * FROM worker_sessions WHERE session_id = ?", (session_id,)).fetchone()
            return self._row_dict(row)
        finally:
            conn.close()

    @staticmethod
    def _physical_source(channel: str, source_chat_id: str) -> str:
        if channel == "telegram":
            from channel.telegram_identity import TelegramAddress

            try:
                return TelegramAddress.parse(source_chat_id).source
            except ValueError as exc:
                raise AgentError("invalid telegram chat") from exc
        return str(source_chat_id or "")

    def select(self, channel: str, source_chat_id: str, operator_id: str, session_id: str) -> dict[str, Any]:
        session = self.get(session_id)
        if session is None or session["archived"]:
            raise AgentError("session is not available")
        self._active_agent(session["agent_id"])
        if session["kind"] == "legacy" and session["operator_id"] != str(operator_id):
            raise AgentError("session is outside this operator")
        source = self._physical_source(str(channel or ""), source_chat_id)
        if not source or not str(operator_id or ""):
            raise AgentError("selection context is incomplete")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO worker_selections (channel, source_chat_id, operator_id, session_id, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(channel, source_chat_id, operator_id) DO UPDATE SET
                         session_id = excluded.session_id, updated_at = excluded.updated_at""",
                    (str(channel), source, str(operator_id), session["session_id"], time.time()),
                )
        finally:
            conn.close()
        return session

    def selected(self, channel: str, source_chat_id: str, operator_id: str) -> dict[str, Any] | None:
        source = self._physical_source(str(channel or ""), source_chat_id)
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT session_id FROM worker_selections
                   WHERE channel = ? AND source_chat_id = ? AND operator_id = ?""",
                (str(channel or ""), source, str(operator_id or "")),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        session = self.get(str(row["session_id"]))
        if session is None or session["archived"]:
            return None
        return session

    def clear(self, channel: str, source_chat_id: str, operator_id: str) -> None:
        source = self._physical_source(str(channel or ""), source_chat_id)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """DELETE FROM worker_selections
                       WHERE channel = ? AND source_chat_id = ? AND operator_id = ?""",
                    (str(channel or ""), source, str(operator_id or "")),
                )
        finally:
            conn.close()

    def issue_token(
        self,
        *,
        operator_id: str,
        channel: str,
        topic: Any = "",
        agent_id: str,
        session_id: str,
        action: str,
        page: int = 0,
        extra: dict[str, Any] | None = None,
        ttl: float = _DEFAULT_TTL,
        now: float | None = None,
    ) -> str:
        self._active_agent(agent_id)
        session = self.get(session_id)
        if session is None or session["archived"] or session["agent_id"] != agent_id:
            raise AgentError("session is not available")
        if not _ACTION_RE.fullmatch(str(action or "")):
            raise AgentError("action is not available")
        payload = extra or {}
        if not isinstance(payload, dict):
            raise AgentError("token extra must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded) > _MAX_EXTRA:
            raise AgentError("token extra is too large")
        stamp = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO worker_callback_tokens
                       (token, operator_id, channel, topic, agent_id, session_id, action, page, extra_json, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        token, str(operator_id), str(channel), str(topic or ""), agent_id, session["session_id"],
                        str(action), int(page), encoded, stamp + float(ttl),
                    ),
                )
        finally:
            conn.close()
        return token

    def resolve_token(
        self,
        token: str,
        *,
        operator_id: str,
        channel: str,
        topic: Any = "",
        agent_id: str,
        session_id: str,
        action: str,
        page: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM worker_callback_tokens WHERE token = ?", (str(token or ""),)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise AgentError("callback token rejected")
        stamp = time.time() if now is None else now
        if float(row["expires_at"]) <= stamp:
            raise AgentError("callback token rejected")
        expected = {
            "operator_id": str(operator_id),
            "channel": str(channel),
            "topic": str(topic or ""),
            "agent_id": str(agent_id),
            "session_id": str(session_id),
            "action": str(action),
        }
        for key, value in expected.items():
            if str(row[key]) != value:
                raise AgentError("callback token rejected")
        if page is not None and int(row["page"]) != int(page):
            raise AgentError("callback token rejected")
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        return {
            "operator_id": row["operator_id"],
            "channel": row["channel"],
            "topic": row["topic"],
            "agent_id": row["agent_id"],
            "session_id": row["session_id"],
            "action": row["action"],
            "page": int(row["page"]),
            "extra": extra,
        }
