"""personal_tools/store.py — SQLite backing store for local personal tools."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import Settings

DB_FILENAME = "personal_tools.db"


def db_path(settings: Settings) -> Path:
    return settings.codex_memory_root / DB_FILENAME


def _connect(settings: Settings) -> sqlite3.Connection:
    path = db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(settings: Settings) -> None:
    with _connect(settings) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notes_operator_created
                ON notes(operator_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id TEXT NOT NULL,
                text TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_operator_due
                ON reminders(operator_id, due_at);
            CREATE INDEX IF NOT EXISTS idx_reminders_status
                ON reminders(operator_id, status, due_at);
            """
        )
        conn.commit()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class NoteRow:
    id: int
    operator_id: str
    text: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReminderRow:
    id: int
    operator_id: str
    text: str
    due_at: str
    status: str
    created_at: str


class PersonalToolsStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        init_db(settings)

    def add_note(self, operator_id: str, text: str) -> NoteRow:
        now = _utc_now()
        with _connect(self._settings) as conn:
            cur = conn.execute(
                "INSERT INTO notes (operator_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (operator_id, text, now, now),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        return NoteRow(id=row_id, operator_id=operator_id, text=text, created_at=now, updated_at=now)

    def search_notes(self, operator_id: str, query: str, *, limit: int = 20) -> list[NoteRow]:
        pattern = f"%{query.strip()}%"
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, created_at, updated_at
                FROM notes
                WHERE operator_id = ? AND text LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (operator_id, pattern, limit),
            ).fetchall()
        return [_note_from_row(r) for r in rows]

    def list_recent_notes(self, operator_id: str, *, limit: int = 10) -> list[NoteRow]:
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, created_at, updated_at
                FROM notes
                WHERE operator_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (operator_id, limit),
            ).fetchall()
        return [_note_from_row(r) for r in rows]

    def delete_note(self, operator_id: str, note_id: int) -> bool:
        with _connect(self._settings) as conn:
            cur = conn.execute(
                "DELETE FROM notes WHERE id = ? AND operator_id = ?",
                (note_id, operator_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_note(self, operator_id: str, note_id: int) -> NoteRow | None:
        with _connect(self._settings) as conn:
            row = conn.execute(
                """
                SELECT id, operator_id, text, created_at, updated_at
                FROM notes WHERE id = ? AND operator_id = ?
                """,
                (note_id, operator_id),
            ).fetchone()
        return _note_from_row(row) if row else None

    def create_reminder(
        self,
        operator_id: str,
        text: str,
        due_at: datetime,
    ) -> ReminderRow:
        now = _utc_now()
        due_iso = due_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                INSERT INTO reminders (operator_id, text, due_at, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (operator_id, text, due_iso, now),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        return ReminderRow(
            id=row_id,
            operator_id=operator_id,
            text=text,
            due_at=due_iso,
            status="pending",
            created_at=now,
        )

    def list_reminders(self, operator_id: str, *, limit: int = 20) -> list[ReminderRow]:
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, due_at, status, created_at
                FROM reminders
                WHERE operator_id = ?
                ORDER BY due_at ASC
                LIMIT ?
                """,
                (operator_id, limit),
            ).fetchall()
        return [_reminder_from_row(r) for r in rows]

    def cancel_reminder(self, operator_id: str, reminder_id: int) -> bool:
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                UPDATE reminders SET status = 'cancelled'
                WHERE id = ? AND operator_id = ? AND status = 'pending'
                """,
                (reminder_id, operator_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_due_reminders(self, operator_id: str, *, now: datetime | None = None) -> list[ReminderRow]:
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, due_at, status, created_at
                FROM reminders
                WHERE operator_id = ? AND status = 'pending' AND due_at <= ?
                ORDER BY due_at ASC
                """,
                (operator_id, ref),
            ).fetchall()
        return [_reminder_from_row(r) for r in rows]


def _note_from_row(row: sqlite3.Row) -> NoteRow:
    return NoteRow(
        id=int(row["id"]),
        operator_id=str(row["operator_id"]),
        text=str(row["text"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _reminder_from_row(row: sqlite3.Row) -> ReminderRow:
    return ReminderRow(
        id=int(row["id"]),
        operator_id=str(row["operator_id"]),
        text=str(row["text"]),
        due_at=str(row["due_at"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )
