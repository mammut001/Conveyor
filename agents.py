"""agents.py — named agents, one conversation each.

An agent is what the operator talks to in the Web Console: it has a name, a
standing instruction, and (in later phases) its own workspace and desktop.
Each agent owns exactly one web conversation, whose chat id is derived from
the agent id, so no session row has to be rewritten to belong to an agent.

Messages from Telegram and Feishu, and web sessions that predate agents, all
belong to the built-in ``default`` agent. It has no instructions unless the
operator sets some, so enabling agents changes nothing until one is created.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from transcript_store import session_identity

DEFAULT_AGENT_ID = "default"
DEFAULT_AGENT_NAME = "Conveyor"
AGENT_CHAT_PREFIX = "agent-"
WEB_CHANNEL = "web"
WEB_OPERATOR = "web-console"
MAX_NAME_CHARS = 60
MAX_INSTRUCTIONS_CHARS = 4000
MAX_PATH_CHARS = 512
MAX_AGENTS = 50
COLORS = ("#2f7df6", "#f59e0b", "#f97316", "#8b5cf6", "#10b981", "#ec4899", "#a16207", "#64748b")
_ID_RE = re.compile(r"^[a-z0-9]{1,32}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class AgentError(ValueError):
    """Operator-facing validation failure; the message is safe to return."""


def enabled(settings: Any) -> bool:
    # `is True`: a mocked or partial settings object must not switch this on.
    return getattr(settings, "agents_enabled", False) is True


def chat_id_for(agent_id: str) -> str:
    return f"{AGENT_CHAT_PREFIX}{agent_id}"


def session_id_for(agent_id: str) -> str:
    """The durable web session that is this agent's one conversation."""
    return session_identity(WEB_CHANNEL, chat_id_for(agent_id), WEB_OPERATOR)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class AgentStore:
    def __init__(self, settings: Any) -> None:
        root = Path(settings.codex_memory_root) / "state"
        root.mkdir(parents=True, exist_ok=True)
        # Same database as sessions and the job queue: one file to back up.
        self.path = root / "job_queue.sqlite3"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS agents (
                           id TEXT PRIMARY KEY,
                           name TEXT NOT NULL,
                           color TEXT NOT NULL,
                           instructions TEXT NOT NULL DEFAULT '',
                           workspace_path TEXT NOT NULL DEFAULT '',
                           display INTEGER,
                           created_at REAL NOT NULL,
                           updated_at REAL NOT NULL,
                           archived INTEGER NOT NULL DEFAULT 0
                       )"""
                )
                now = time.time()
                conn.execute(
                    """INSERT OR IGNORE INTO agents (id, name, color, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (DEFAULT_AGENT_ID, DEFAULT_AGENT_NAME, COLORS[2], now, now),
                )
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        agent = dict(row)
        agent["archived"] = bool(agent["archived"])
        agent["is_default"] = agent["id"] == DEFAULT_AGENT_ID
        agent["session_id"] = session_id_for(agent["id"])
        return agent

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            where = "" if include_archived else "WHERE archived = 0"
            rows = conn.execute(f"SELECT * FROM agents {where} ORDER BY created_at ASC").fetchall()
            return [self._row(row) for row in rows]  # type: ignore[misc]
        finally:
            conn.close()

    def get(self, agent_id: str) -> dict[str, Any] | None:
        if not _ID_RE.match(str(agent_id or "")):
            return None
        conn = self._connect()
        try:
            return self._row(conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone())
        finally:
            conn.close()

    @staticmethod
    def _validated(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if "name" in payload or not partial:
            name = _clean(payload.get("name"), MAX_NAME_CHARS + 1)
            if not name or len(name) > MAX_NAME_CHARS:
                raise AgentError(f"name must be 1-{MAX_NAME_CHARS} characters")
            fields["name"] = name
        if "instructions" in payload:
            instructions = str(payload.get("instructions") or "").strip()
            if len(instructions) > MAX_INSTRUCTIONS_CHARS:
                raise AgentError(f"instructions must be at most {MAX_INSTRUCTIONS_CHARS} characters")
            fields["instructions"] = instructions
        if "workspace_path" in payload:
            workspace = str(payload.get("workspace_path") or "").strip()
            if workspace:
                if len(workspace) > MAX_PATH_CHARS or not workspace.startswith("/") or "\n" in workspace:
                    raise AgentError("workspace_path must be an absolute path")
                workspace = str(Path(workspace))
            fields["workspace_path"] = workspace
        if "color" in payload:
            color = str(payload.get("color") or "").strip()
            if not _COLOR_RE.match(color):
                raise AgentError("color must look like #rrggbb")
            fields["color"] = color.lower()
        return fields

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = self._validated(payload, partial=False)
        now = time.time()
        agent_id = uuid.uuid4().hex[:10]
        conn = self._connect()
        try:
            with conn:
                count = conn.execute("SELECT COUNT(*) FROM agents WHERE archived = 0").fetchone()[0]
                if count >= MAX_AGENTS:
                    raise AgentError(f"at most {MAX_AGENTS} agents")
                color = fields.get("color") or COLORS[count % len(COLORS)]
                conn.execute(
                    """INSERT INTO agents (id, name, color, instructions, workspace_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (agent_id, fields["name"], color, fields.get("instructions", ""),
                     fields.get("workspace_path", ""), now, now),
                )
        finally:
            conn.close()
        return self.get(agent_id) or {}

    def update(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.get(agent_id) is None:
            return None
        fields = self._validated(payload, partial=True)
        if fields:
            fields["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in fields)
            conn = self._connect()
            try:
                with conn:
                    conn.execute(f"UPDATE agents SET {assignments} WHERE id = ?", (*fields.values(), agent_id))
            finally:
                conn.close()
        return self.get(agent_id)

    def archive(self, agent_id: str) -> bool:
        """Hide an agent. Its conversation and files are kept."""
        if agent_id == DEFAULT_AGENT_ID:
            raise AgentError("the default agent cannot be removed")
        if self.get(agent_id) is None:
            return False
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE agents SET archived = 1, updated_at = ? WHERE id = ?", (time.time(), agent_id),
                )
        finally:
            conn.close()
        return True


def agent_for_chat(settings: Any, channel: str, chat_id: str) -> dict[str, Any] | None:
    """The agent a conversation belongs to, or None when agents are off."""
    if not enabled(settings):
        return None
    store = AgentStore(settings)
    chat_id = str(chat_id or "")
    if channel == WEB_CHANNEL and chat_id.startswith(AGENT_CHAT_PREFIX):
        agent = store.get(chat_id[len(AGENT_CHAT_PREFIX):])
        # An archived or unknown agent has no say over a conversation.
        return agent if agent and not agent["archived"] else None
    return store.get(DEFAULT_AGENT_ID)


def instructions_for_chat(settings: Any, channel: str, chat_id: str) -> tuple[str, str]:
    """(agent name, instructions) for a conversation; empty when nothing applies."""
    try:
        agent = agent_for_chat(settings, channel, chat_id)
    except (OSError, sqlite3.Error):
        return "", ""
    if not agent or not agent["instructions"]:
        return "", ""
    return agent["name"], agent["instructions"]


def profile_block(name: str, instructions: str) -> str:
    """Prompt block for the execution tier; empty when there is nothing to say."""
    if not instructions:
        return ""
    safe_name = name.replace('"', "'")
    return (
        f'<agent-profile name="{safe_name}" source="operator">\n'
        "Standing instructions the operator gave this agent. They describe its role and "
        "apply to every request in this conversation.\n"
        f"{instructions}\n"
        "</agent-profile>\n\n"
    )
