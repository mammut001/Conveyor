"""agents.py — named agents, one conversation each.

An agent is what the operator talks to in the Web Console: it has a name, a
standing instruction, and (in later phases) its own workspace and desktop.
Each agent owns exactly one web conversation, whose chat id is derived from
the agent id, so no session row has to be rewritten to belong to an agent.

Unbound Telegram chats, Feishu, and web sessions that predate agents all
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
# X display numbers for agent desktops start above anything a login session uses.
DISPLAY_BASE = 100
MAX_DISPLAY = 999
COLORS = ("#2f7df6", "#f59e0b", "#f97316", "#8b5cf6", "#10b981", "#ec4899", "#a16207", "#64748b")
_ID_RE = re.compile(r"^[a-z0-9]{1,32}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class AgentError(ValueError):
    """Operator-facing validation failure; the message is safe to return."""


def enabled(settings: Any) -> bool:
    # `is True`: a mocked or partial settings object must not switch this on.
    return getattr(settings, "agents_enabled", False) is True


def desktops_enabled(settings: Any) -> bool:
    return enabled(settings) and getattr(settings, "agent_desktops_enabled", False) is True


def desktop_dir(settings: Any, agent_id: str) -> Path:
    """Private state of one agent's desktop (X authority, pids, requests)."""
    return Path(settings.codex_memory_root) / "state" / "agent_desktops" / agent_id


def xauthority_path(settings: Any, agent_id: str) -> Path:
    """The cookie file the agent's X server checks clients against."""
    return desktop_dir(settings, agent_id) / "Xauthority"


def client_xauthority_path() -> Path:
    """Where programs find the cookies of every agent display.

    The user's standard ~/.Xauthority, one entry per display number. It has to
    be that file: sandboxed (snap) browsers may read it but nothing under a
    hidden directory, so a per-agent file in the state directory is invisible
    to them.
    """
    return Path.home() / ".Xauthority"


def takeover_scope(agent_id: str) -> str:
    """Takeover lease scope of an agent's own desktop."""
    return f"agent:{agent_id}"


def computer_target_for_chat(settings: Any, channel: str, chat_id: str) -> dict[str, Any]:
    """Which desktop a conversation's computer use acts on.

    ``{"scope": "default"}`` is the shared host desktop. An agent with its own
    desktop gets ``{"scope": "agent:<id>", "agent_id": ..., "display": N}``.
    """
    host = {"scope": "default"}
    if not desktops_enabled(settings):
        return host
    try:
        agent = agent_for_chat(settings, channel, chat_id)
    except (OSError, sqlite3.Error) as exc:
        if channel == "telegram" and ":agent:" in str(chat_id):
            raise AgentError("暂时无法读取此 Agent 的桌面配置，请稍后重试。") from exc
        return host
    if not agent or agent.get("display") is None:
        return host
    return {"scope": takeover_scope(agent["id"]), "agent_id": agent["id"], "display": int(agent["display"])}


def workspace_for_chat(settings: Any, channel: str, chat_id: str) -> Path | None:
    """The git repository an agent's jobs run in, or None for the default one."""
    try:
        agent = agent_for_chat(settings, channel, chat_id)
    except (OSError, sqlite3.Error) as exc:
        if channel == "telegram" and ":agent:" in str(chat_id):
            raise AgentError("暂时无法读取此 Agent 的项目配置，请稍后重试。") from exc
        return None
    if not agent or not agent.get("workspace_path"):
        return None
    return Path(agent["workspace_path"])


def settings_for_chat(settings: Any, channel: str, chat_id: str) -> Any:
    """Project-scoped settings for read-only tool batches; state roots stay shared."""
    from dataclasses import replace
    workspace = workspace_for_chat(settings, channel, chat_id)
    return replace(settings, codex_workspace_root=workspace) if workspace is not None else settings


def workspace_roots(settings: Any) -> set[Path]:
    """Every agent's project folder (resolved). The runner only ever applies
    changes into the configured workspace or one of these."""
    if not enabled(settings):
        return set()
    roots: set[Path] = set()
    try:
        for agent in AgentStore(settings).list():
            if agent.get("workspace_path"):
                try:
                    roots.add(Path(agent["workspace_path"]).resolve())
                except OSError:
                    continue
    except (OSError, sqlite3.Error):
        return set()
    return roots


def chat_id_for(agent_id: str) -> str:
    return f"{AGENT_CHAT_PREFIX}{agent_id}"


def session_id_for(agent_id: str) -> str:
    """The durable web session that is this agent's one conversation."""
    return session_identity(WEB_CHANNEL, chat_id_for(agent_id), WEB_OPERATOR)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


# Databases whose agents table this process has already created. Stores are
# constructed on every lookup; taking a write lock each time would make them
# wait on (or stall) the job queue's transactions in the same file.
_initialised: set[str] = set()


class AgentStore:
    def __init__(self, settings: Any) -> None:
        root = Path(settings.codex_memory_root) / "state"
        root.mkdir(parents=True, exist_ok=True)
        # Same database as sessions and the job queue: one file to back up.
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
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS agent_chat_bindings (
                           channel TEXT NOT NULL,
                           chat_id TEXT NOT NULL,
                           agent_id TEXT NOT NULL REFERENCES agents(id),
                           created_at REAL NOT NULL,
                           PRIMARY KEY (channel, chat_id)
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

    def ensure_display(self, agent_id: str) -> int | None:
        """Give an agent an X display number if it has none; return it.

        The default agent keeps using the host's own desktop and never gets one.
        """
        if agent_id == DEFAULT_AGENT_ID:
            return None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT display, archived FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None or row["archived"]:
                conn.rollback()
                return None
            if row["display"] is not None:
                conn.rollback()
                return int(row["display"])
            taken = {int(r[0]) for r in conn.execute(
                "SELECT display FROM agents WHERE display IS NOT NULL AND archived = 0"
            )}
            display = next((n for n in range(DISPLAY_BASE + 1, MAX_DISPLAY + 1) if n not in taken), None)
            if display is None:
                conn.rollback()
                return None
            conn.execute("UPDATE agents SET display = ? WHERE id = ?", (display, agent_id))
            conn.commit()
            return display
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def bound_agent_id(self, channel: str, chat_id: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT agent_id FROM agent_chat_bindings WHERE channel = ? AND chat_id = ?",
                (channel, str(chat_id)),
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    def bind_chat(self, channel: str, chat_id: str, agent_id: str | None) -> None:
        """Select an agent; scoped jobs keep their original agent identity.

        Legacy jobs/worktrees without an agent suffix must finish first,
        because older versions resolved their project through this binding.
        """
        from channel.telegram_identity import source_address
        if channel != "telegram":
            raise AgentError("目前仅支持 Telegram 对话绑定。")
        try:
            source = source_address(chat_id)
        except ValueError as exc:
            raise AgentError("无效的 Telegram 对话。") from exc
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                agent = conn.execute(
                    "SELECT archived FROM agents WHERE id = ?", (agent_id,),
                ).fetchone()
                if agent_id is not None and (agent is None or agent[0]):
                    raise AgentError("Agent 不存在或已归档，请用 /agent list 查看。")
                row = conn.execute(
                    "SELECT agent_id FROM agent_chat_bindings WHERE channel = ? AND chat_id = ?",
                    (channel, source),
                ).fetchone()
                if (row and row[0] == agent_id) or (not row and agent_id is None):
                    return
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "queued_jobs" in tables and conn.execute(
                    "SELECT 1 FROM queued_jobs WHERE channel = ? AND chat_id = ? AND state IN ('queued','running') LIMIT 1",
                    (channel, source),
                ).fetchone():
                    raise AgentError("请先完成或取消此对话中旧版的排队和运行任务，再切换 Agent。")
                if "session_worktrees" in tables and conn.execute(
                    "SELECT 1 FROM session_worktrees WHERE channel = ? AND source_chat_id = ? AND state = 'active' LIMIT 1",
                    (channel, source),
                ).fetchone():
                    raise AgentError("请先 /apply 或 /discard 此对话中旧版的 worktree，再切换 Agent。")
                if agent_id is None:
                    conn.execute("DELETE FROM agent_chat_bindings WHERE channel = ? AND chat_id = ?", (channel, source))
                    return
                conn.execute(
                    """INSERT INTO agent_chat_bindings VALUES (?, ?, ?, ?)
                       ON CONFLICT(channel, chat_id) DO UPDATE SET
                       agent_id = excluded.agent_id, created_at = excluded.created_at""",
                    (channel, source, agent_id, time.time()),
                )
        finally:
            conn.close()

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
                    "UPDATE agents SET archived = 1, display = NULL, updated_at = ? WHERE id = ?",
                    (time.time(), agent_id),
                )
        finally:
            conn.close()
        return True


def agent_for_chat(settings: Any, channel: str, chat_id: str) -> dict[str, Any] | None:
    """The agent a conversation belongs to, or None when agents are off."""
    if not enabled(settings):
        if channel == "telegram" and ":agent:" in str(chat_id):
            raise AgentError("此任务属于已绑定的 Agent，但 Agent 功能已关闭。请重新开启后处理，任务不会转到默认项目。")
        return None
    store = AgentStore(settings)
    chat_id = str(chat_id or "")
    if channel == WEB_CHANNEL:
        from worker_sessions import WorkerSessionStore

        sessions = WorkerSessionStore(settings)
        owned = sessions.owner_agent_id(WEB_CHANNEL, chat_id)
        if owned is not None:
            agent = store.get(owned)
            # Archived, disabled, or missing registry targets do not fall through.
            return agent if agent and not agent["archived"] else None
        if sessions.is_secondary_chat_id(chat_id):
            return None
        if chat_id.startswith(AGENT_CHAT_PREFIX):
            agent = store.get(chat_id[len(AGENT_CHAT_PREFIX):])
            # An archived or unknown agent has no say over a conversation.
            return agent if agent and not agent["archived"] else None
    if channel == "telegram":
        from channel.telegram_identity import TelegramAddress
        try:
            address = TelegramAddress.parse(chat_id)
        except ValueError:
            address = None  # Non-Telegram synthetic IDs in legacy callers.
        bound = address.agent_id if address else None
        if not bound:
            bound = store.bound_agent_id(channel, address.source if address else chat_id)
        if bound:
            agent = store.get(bound)
            if not agent or agent["archived"]:
                raise AgentError("此对话的 Agent 已归档。用 /agent 选择其他 Agent；旧任务不会自动转移到其他项目。")
            return agent
    return store.get(DEFAULT_AGENT_ID)


def conversation_for_chat(settings: Any, channel: str, chat_id: str, *, validate: bool = True) -> str:
    """Pin the selected agent into the durable Telegram conversation address."""
    if channel != "telegram" or not enabled(settings):
        return chat_id
    from channel.telegram_identity import TelegramAddress
    try:
        address = TelegramAddress.parse(chat_id)
    except ValueError:
        return chat_id
    if address.agent_id:
        if validate:
            agent_for_chat(settings, channel, chat_id)
        return chat_id
    bound = AgentStore(settings).bound_agent_id(channel, address.source)
    if not bound:
        return chat_id
    if validate:
        agent_for_chat(settings, channel, chat_id)
    return TelegramAddress(address.chat_id, address.topic_id, bound).conversation


def instructions_for_chat(settings: Any, channel: str, chat_id: str) -> tuple[str, str]:
    """(agent name, instructions) for a conversation; empty when nothing applies."""
    try:
        agent = agent_for_chat(settings, channel, chat_id)
    except (OSError, sqlite3.Error) as exc:
        if channel == "telegram" and ":agent:" in str(chat_id):
            raise AgentError("暂时无法读取此 Agent 的配置，请稍后重试。") from exc
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
