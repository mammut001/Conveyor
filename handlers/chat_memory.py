"""handlers/chat_memory.py — SQLite-backed persistent chat memory.

Persists conversation turns, session state, and /deep escalation requests
across bot restarts under ``settings.codex_memory_root / "chat_memory.db"``.

Thread history is indexed by `(chat_key, created_at)`.
Old raw turns beyond retention limits are pruned automatically.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)

DB_NAME = "chat_memory.db"
DEFAULT_SESSION_TTL_SECONDS = 24 * 3600  # 24 hours retention for active conversation window
MAX_STORED_TURNS_PER_CHAT = 60  # max turns kept in database per chat_key


@dataclass(frozen=True)
class StoredLastRequest:
    codex_prompt: str
    confirm: bool


_known_roots: set[Path] = set()


def _db_path(memory_root: Path) -> Path:
    return memory_root / DB_NAME


def _connect(memory_root: Path) -> sqlite3.Connection:
    memory_root.mkdir(parents=True, exist_ok=True)
    _known_roots.add(memory_root.resolve())
    path = _db_path(memory_root)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_key TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_chat_turns_key_time
        ON chat_turns (chat_key, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_last (
            chat_key TEXT PRIMARY KEY,
            codex_prompt TEXT NOT NULL,
            confirm INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_facts (
            chat_key TEXT PRIMARY KEY,
            facts TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.commit()


def get_history(
    memory_root: Path,
    chat_key: str,
    limit: int,
    *,
    ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
    now: float | None = None,
) -> list[dict[str, str]]:
    """Return the recent conversation turns for `chat_key`.

    If the newest turn is older than `ttl_seconds`, the active conversation window
    is considered expired and an empty list is returned.
    """
    now = time.time() if now is None else now
    try:
        with _connect(memory_root) as conn:
            # Check latest turn timestamp for session TTL
            latest = conn.execute(
                "SELECT created_at FROM chat_turns WHERE chat_key = ? ORDER BY id DESC LIMIT 1",
                (chat_key,),
            ).fetchone()
            if not latest:
                return []
            if now - float(latest["created_at"]) > ttl_seconds:
                return []

            # Fetch the most recent 2 * limit turns
            target_count = 2 * max(0, limit)
            if target_count == 0:
                return []
            rows = conn.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM chat_turns
                    WHERE chat_key = ?
                    ORDER BY id DESC
                    LIMIT ?
                ) ORDER BY id ASC
                """,
                (chat_key, target_count),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as exc:
        logger.warning("Failed to load chat history for %s: %s", chat_key, exc)
        return []


def add_turn(
    memory_root: Path,
    chat_key: str,
    user_content: str,
    assistant_content: str,
    limit: int,
    *,
    now: float | None = None,
) -> None:
    """Record a user-assistant exchange into the persistent database and prune old turns."""
    now = time.time() if now is None else now
    try:
        with _connect(memory_root) as conn:
            conn.execute(
                "INSERT INTO chat_turns (chat_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_key, "user", user_content, now),
            )
            conn.execute(
                "INSERT INTO chat_turns (chat_key, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_key, "assistant", assistant_content, now),
            )
            # Prune turns older than MAX_STORED_TURNS_PER_CHAT
            conn.execute(
                """
                DELETE FROM chat_turns
                WHERE chat_key = ? AND id NOT IN (
                    SELECT id FROM chat_turns
                    WHERE chat_key = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (chat_key, chat_key, MAX_STORED_TURNS_PER_CHAT),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to save chat turn for %s: %s", chat_key, exc)


def save_last_request(
    memory_root: Path,
    chat_key: str,
    request: StoredLastRequest,
    *,
    now: float | None = None,
) -> None:
    now = time.time() if now is None else now
    try:
        with _connect(memory_root) as conn:
            conn.execute(
                """
                INSERT INTO chat_last (chat_key, codex_prompt, confirm, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    codex_prompt = excluded.codex_prompt,
                    confirm = excluded.confirm,
                    updated_at = excluded.updated_at
                """,
                (chat_key, request.codex_prompt, 1 if request.confirm else 0, now),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to save last request for %s: %s", chat_key, exc)


def pop_last_request(memory_root: Path, chat_key: str) -> StoredLastRequest | None:
    try:
        with _connect(memory_root) as conn:
            row = conn.execute(
                "SELECT codex_prompt, confirm FROM chat_last WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM chat_last WHERE chat_key = ?", (chat_key,))
            conn.commit()
            return StoredLastRequest(
                codex_prompt=row["codex_prompt"],
                confirm=bool(row["confirm"]),
            )
    except Exception as exc:
        logger.warning("Failed to pop last request for %s: %s", chat_key, exc)
        return None


def clear_history(memory_root: Path | None = None, chat_key: str | None = None) -> None:
    """Clear history for a specific chat_key, or all chats if None."""
    roots = [memory_root] if memory_root is not None else list(_known_roots)
    for root in roots:
        try:
            with _connect(root) as conn:
                if chat_key is None:
                    conn.execute("DELETE FROM chat_turns")
                    conn.execute("DELETE FROM chat_last")
                    conn.execute("DELETE FROM chat_facts")
                else:
                    conn.execute("DELETE FROM chat_turns WHERE chat_key = ?", (chat_key,))
                    conn.execute("DELETE FROM chat_last WHERE chat_key = ?", (chat_key,))
                    conn.execute("DELETE FROM chat_facts WHERE chat_key = ?", (chat_key,))
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to clear chat history for %s: %s", chat_key, exc)


def get_facts(memory_root: Path, chat_key: str) -> str:
    """Retrieve long-term summarized facts for this chat."""
    try:
        with _connect(memory_root) as conn:
            row = conn.execute(
                "SELECT facts FROM chat_facts WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
            return str(row["facts"]) if row else ""
    except Exception:
        return ""


def set_facts(memory_root: Path, chat_key: str, facts: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        with _connect(memory_root) as conn:
            conn.execute(
                """
                INSERT INTO chat_facts (chat_key, facts, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    facts = excluded.facts,
                    updated_at = excluded.updated_at
                """,
                (chat_key, facts, now),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to set chat facts for %s: %s", chat_key, exc)
