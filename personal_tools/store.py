"""personal_tools/store.py — SQLite backing store for local personal tools."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import Settings

logger = logging.getLogger(__name__)

DB_FILENAME = "personal_tools.db"

# Columns present since P3.1 initial schema.
_REMINDER_BASE_COLUMNS = {
    "id", "operator_id", "text", "due_at", "status", "created_at",
}
# Columns added by P3.2 delivery migration.
_REMINDER_DELIVERY_COLUMNS = {
    "channel", "chat_id", "delivered_at", "delivery_status",
    "delivery_error", "retry_count",
}


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


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_reminders(conn: sqlite3.Connection) -> None:
    """Add delivery columns to existing reminders table if missing."""
    existing = _existing_columns(conn, "reminders")
    needed = _REMINDER_DELIVERY_COLUMNS - existing
    if not needed:
        return
    for col in sorted(needed):
        if col in ("channel", "chat_id"):
            conn.execute(f"ALTER TABLE reminders ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        elif col == "delivered_at":
            conn.execute(f"ALTER TABLE reminders ADD COLUMN delivered_at TEXT")
        elif col == "delivery_status":
            conn.execute(f"ALTER TABLE reminders ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'")
        elif col == "delivery_error":
            conn.execute(f"ALTER TABLE reminders ADD COLUMN delivery_error TEXT")
        elif col == "retry_count":
            conn.execute(f"ALTER TABLE reminders ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    logger.info("Migrated reminders table: added %s", sorted(needed))


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
                created_at TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                delivered_at TEXT,
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                delivery_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_operator_due
                ON reminders(operator_id, due_at);
            CREATE INDEX IF NOT EXISTS idx_reminders_status
                ON reminders(operator_id, status, due_at);
            """
        )
        conn.commit()
        _migrate_reminders(conn)
        # Delivery index must exist after migration adds the column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_delivery "
            "ON reminders(delivery_status, status, due_at)"
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
    channel: str = ""
    chat_id: str = ""
    delivered_at: str | None = None
    delivery_status: str = "pending"
    delivery_error: str | None = None
    retry_count: int = 0


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
        *,
        channel: str = "",
        chat_id: str = "",
    ) -> ReminderRow:
        now = _utc_now()
        due_iso = due_at.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                INSERT INTO reminders
                    (operator_id, text, due_at, status, created_at, channel, chat_id,
                     delivery_status, retry_count)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, 'pending', 0)
                """,
                (operator_id, text, due_iso, now, channel, chat_id),
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
            channel=channel,
            chat_id=chat_id,
            delivery_status="pending",
            retry_count=0,
        )

    def list_reminders(self, operator_id: str, *, limit: int = 20) -> list[ReminderRow]:
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, due_at, status, created_at,
                       channel, chat_id, delivered_at, delivery_status,
                       delivery_error, retry_count
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
                SELECT id, operator_id, text, due_at, status, created_at,
                       channel, chat_id, delivered_at, delivery_status,
                       delivery_error, retry_count
                FROM reminders
                WHERE operator_id = ? AND status = 'pending' AND due_at <= ?
                ORDER BY due_at ASC
                """,
                (operator_id, ref),
            ).fetchall()
        return [_reminder_from_row(r) for r in rows]

    def list_due_deliverable_reminders(self, *, now: datetime | None = None) -> list[ReminderRow]:
        """Return all pending reminders that are due and have channel/chat_id set."""
        ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with _connect(self._settings) as conn:
            rows = conn.execute(
                """
                SELECT id, operator_id, text, due_at, status, created_at,
                       channel, chat_id, delivered_at, delivery_status,
                       delivery_error, retry_count
                FROM reminders
                WHERE status = 'pending'
                  AND delivery_status IN ('pending', 'failed')
                  AND due_at <= ?
                  AND channel != ''
                  AND chat_id != ''
                  AND retry_count < 3
                ORDER BY due_at ASC
                """,
                (ref,),
            ).fetchall()
        return [_reminder_from_row(r) for r in rows]

    def mark_reminder_done(self, reminder_id: int, delivered_at: str) -> bool:
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                UPDATE reminders
                SET delivery_status = 'delivered', delivered_at = ?, status = 'done'
                WHERE id = ? AND status = 'pending'
                """,
                (delivered_at, reminder_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_reminder_failed(self, reminder_id: int, error: str, retry_count: int) -> bool:
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                UPDATE reminders
                SET delivery_status = 'failed', delivery_error = ?, retry_count = ?
                WHERE id = ? AND status = 'pending'
                """,
                (error, retry_count, reminder_id),
            )
            conn.commit()
            return cur.rowcount > 0


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
        channel=str(row["channel"]) if "channel" in row.keys() else "",
        chat_id=str(row["chat_id"]) if "chat_id" in row.keys() else "",
        delivered_at=str(row["delivered_at"]) if row["delivered_at"] is not None else None,
        delivery_status=str(row["delivery_status"]) if "delivery_status" in row.keys() else "pending",
        delivery_error=str(row["delivery_error"]) if row["delivery_error"] is not None else None,
        retry_count=int(row["retry_count"]) if "retry_count" in row.keys() else 0,
    )
