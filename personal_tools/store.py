"""personal_tools/store.py — SQLite backing store for local personal tools."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
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

# P3.5 Daily Briefing schema constants.
BRIEFING_SETTINGS_COLUMNS = {
    "operator_id", "enabled", "local_time", "channel", "chat_id",
    "created_at", "updated_at",
}
BRIEFING_RUNS_COLUMNS = {
    "id", "operator_id", "local_date", "status", "sent_at", "error",
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

            -- P3.5 Daily Briefing tables
            CREATE TABLE IF NOT EXISTS briefing_settings (
                operator_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                local_time TEXT NOT NULL DEFAULT '09:00',
                channel TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS briefing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                sent_at TEXT NOT NULL,
                error TEXT,
                UNIQUE(operator_id, local_date)
            );
            CREATE INDEX IF NOT EXISTS idx_briefing_runs_operator_date
                ON briefing_runs(operator_id, local_date);

            -- P3.9 Project Profiles tables
            CREATE TABLE IF NOT EXISTS project_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'generic',
                description TEXT NOT NULL DEFAULT '',
                github_repo TEXT NOT NULL DEFAULT '',
                appstore_url TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '[]',
                notes_query TEXT NOT NULL DEFAULT '',
                gmail_query TEXT NOT NULL DEFAULT '',
                default_branch TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_project_profiles_operator
                ON project_profiles(operator_id, enabled);

            CREATE TABLE IF NOT EXISTS active_projects (
                operator_id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL,
                FOREIGN KEY (project_id) REFERENCES project_profiles(id)
            );
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


@dataclass(frozen=True)
class BriefingSettingsRow:
    operator_id: str
    enabled: bool
    local_time: str
    channel: str
    chat_id: str
    created_at: str
    updated_at: str


# Valid project types
PROJECT_TYPES = ("generic", "mobile_app", "web_app", "bot", "library", "research", "course", "business")


@dataclass(frozen=True)
class ProjectProfileRow:
    id: int
    operator_id: str
    name: str
    type: str
    description: str
    github_repo: str
    appstore_url: str
    keywords: tuple[str, ...]
    notes_query: str
    gmail_query: str
    default_branch: str
    enabled: bool
    created_at: str
    updated_at: str


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

    # --- P3.5 Daily Briefing methods ---

    def get_briefing_settings(self, operator_id: str) -> BriefingSettingsRow | None:
        with _connect(self._settings) as conn:
            row = conn.execute(
                "SELECT * FROM briefing_settings WHERE operator_id = ?",
                (operator_id,),
            ).fetchone()
        return _briefing_settings_from_row(row) if row else None

    def update_briefing_settings(
        self,
        operator_id: str,
        *,
        enabled: bool,
        local_time: str = "09:00",
        channel: str = "",
        chat_id: str = "",
    ) -> BriefingSettingsRow:
        now = _utc_now()
        with _connect(self._settings) as conn:
            conn.execute(
                """
                INSERT INTO briefing_settings (operator_id, enabled, local_time, channel, chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operator_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    local_time = excluded.local_time,
                    channel = excluded.channel,
                    chat_id = excluded.chat_id,
                    updated_at = excluded.updated_at
                """,
                (operator_id, int(enabled), local_time, channel, chat_id, now, now),
            )
            conn.commit()
        return BriefingSettingsRow(
            operator_id=operator_id,
            enabled=enabled,
            local_time=local_time,
            channel=channel,
            chat_id=chat_id,
            created_at=now,
            updated_at=now,
        )

    def has_briefing_run_for_date(self, operator_id: str, local_date: str) -> bool:
        with _connect(self._settings) as conn:
            row = conn.execute(
                "SELECT 1 FROM briefing_runs WHERE operator_id = ? AND local_date = ?",
                (operator_id, local_date),
            ).fetchone()
        return row is not None

    def record_briefing_run(
        self,
        operator_id: str,
        local_date: str,
        *,
        status: str = "sent",
        error: str | None = None,
    ) -> int:
        sent_at = _utc_now()
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                INSERT INTO briefing_runs (operator_id, local_date, status, sent_at, error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(operator_id, local_date) DO UPDATE SET
                    status = excluded.status,
                    sent_at = excluded.sent_at,
                    error = excluded.error
                """,
                (operator_id, local_date, status, sent_at, error),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_enabled_briefings(self) -> list[BriefingSettingsRow]:
        with _connect(self._settings) as conn:
            rows = conn.execute(
                "SELECT * FROM briefing_settings WHERE enabled = 1"
            ).fetchall()
        return [_briefing_settings_from_row(r) for r in rows]

    # --- P3.9 Project Profile methods ---

    def create_project_profile(
        self,
        operator_id: str,
        name: str,
        project_type: str = "generic",
        description: str = "",
        github_repo: str = "",
        appstore_url: str = "",
        keywords: tuple[str, ...] = (),
        notes_query: str = "",
        gmail_query: str = "",
        default_branch: str = "",
        enabled: bool = True,
    ) -> ProjectProfileRow:
        now = _utc_now()
        keywords_json = json.dumps(list(keywords), ensure_ascii=False)
        enabled_int = 1 if enabled else 0
        with _connect(self._settings) as conn:
            cur = conn.execute(
                """
                INSERT INTO project_profiles
                    (operator_id, name, type, description, github_repo, appstore_url,
                     keywords, notes_query, gmail_query, default_branch, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (operator_id, name, project_type, description, github_repo,
                 appstore_url, keywords_json, notes_query, gmail_query,
                 default_branch, enabled_int, now, now),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        return ProjectProfileRow(
            id=row_id, operator_id=operator_id, name=name, type=project_type,
            description=description, github_repo=github_repo, appstore_url=appstore_url,
            keywords=keywords, notes_query=notes_query, gmail_query=gmail_query,
            default_branch=default_branch, enabled=enabled, created_at=now, updated_at=now,
        )

    def update_project_profile(
        self,
        operator_id: str,
        project_id: int,
        **kwargs,
    ) -> bool:
        allowed = {"name", "type", "description", "github_repo", "appstore_url",
                    "keywords", "notes_query", "gmail_query", "default_branch", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return False
        if "keywords" in updates and isinstance(updates["keywords"], (list, tuple)):
            updates["keywords"] = json.dumps(list(updates["keywords"]), ensure_ascii=False)
        if "enabled" in updates:
            updates["enabled"] = int(bool(updates["enabled"]))
        now = _utc_now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [now, operator_id, project_id]
        with _connect(self._settings) as conn:
            cur = conn.execute(
                f"UPDATE project_profiles SET {set_clause}, updated_at = ? "
                "WHERE operator_id = ? AND id = ?",
                values,
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_project_profile(self, operator_id: str, project_id: int) -> bool:
        with _connect(self._settings) as conn:
            # Remove from active_projects first
            conn.execute(
                "DELETE FROM active_projects WHERE operator_id = ? AND project_id = ?",
                (operator_id, project_id),
            )
            cur = conn.execute(
                "DELETE FROM project_profiles WHERE operator_id = ? AND id = ?",
                (operator_id, project_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_project_profiles(self, operator_id: str) -> list[ProjectProfileRow]:
        with _connect(self._settings) as conn:
            rows = conn.execute(
                "SELECT * FROM project_profiles WHERE operator_id = ? ORDER BY created_at DESC",
                (operator_id,),
            ).fetchall()
        return [_project_profile_from_row(r) for r in rows]

    def get_project_profile(self, operator_id: str, project_id: int) -> ProjectProfileRow | None:
        with _connect(self._settings) as conn:
            row = conn.execute(
                "SELECT * FROM project_profiles WHERE operator_id = ? AND id = ?",
                (operator_id, project_id),
            ).fetchone()
        return _project_profile_from_row(row) if row else None

    def set_active_project(self, operator_id: str, project_id: int) -> bool:
        with _connect(self._settings) as conn:
            # Verify project exists
            row = conn.execute(
                "SELECT 1 FROM project_profiles WHERE operator_id = ? AND id = ?",
                (operator_id, project_id),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                """
                INSERT INTO active_projects (operator_id, project_id)
                VALUES (?, ?)
                ON CONFLICT(operator_id) DO UPDATE SET project_id = excluded.project_id
                """,
                (operator_id, project_id),
            )
            conn.commit()
            return True

    def get_active_project(self, operator_id: str) -> ProjectProfileRow | None:
        with _connect(self._settings) as conn:
            row = conn.execute(
                """
                SELECT p.* FROM project_profiles p
                JOIN active_projects a ON p.id = a.project_id
                WHERE a.operator_id = ?
                """,
                (operator_id,),
            ).fetchone()
        return _project_profile_from_row(row) if row else None

    def get_active_or_first_project(self, operator_id: str) -> ProjectProfileRow | None:
        """Get active project, or first enabled project if no active set."""
        active = self.get_active_project(operator_id)
        if active:
            return active
        with _connect(self._settings) as conn:
            row = conn.execute(
                "SELECT * FROM project_profiles WHERE operator_id = ? AND enabled = 1 "
                "ORDER BY created_at DESC LIMIT 1",
                (operator_id,),
            ).fetchone()
        return _project_profile_from_row(row) if row else None


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


def _briefing_settings_from_row(row: sqlite3.Row) -> BriefingSettingsRow:
    return BriefingSettingsRow(
        operator_id=str(row["operator_id"]),
        enabled=bool(row["enabled"]),
        local_time=str(row["local_time"]),
        channel=str(row["channel"]),
        chat_id=str(row["chat_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _project_profile_from_row(row: sqlite3.Row) -> ProjectProfileRow:
    keywords_raw = str(row["keywords"])
    try:
        keywords = tuple(json.loads(keywords_raw)) if keywords_raw else ()
    except (json.JSONDecodeError, TypeError):
        keywords = ()
    return ProjectProfileRow(
        id=int(row["id"]),
        operator_id=str(row["operator_id"]),
        name=str(row["name"]),
        type=str(row["type"]),
        description=str(row["description"]),
        github_repo=str(row["github_repo"]),
        appstore_url=str(row["appstore_url"]),
        keywords=keywords,
        notes_query=str(row["notes_query"]),
        gmail_query=str(row["gmail_query"]),
        default_branch=str(row["default_branch"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
