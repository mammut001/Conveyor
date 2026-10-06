"""Persistent, secret-free state for human takeover sessions.

Human takeover is the explicit boundary between Conveyor's automated computer-use
loop and sensitive GUI work that must be performed by the operator (payments,
passwords, CAPTCHA, identity checks, etc.).

This module intentionally stores *coordination metadata only*. It never stores
screenshots, typed text, credentials, payment data, cookies, or VNC passwords.
The remote-desktop transport is a separate concern.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

OPEN_STATES = ("waiting_for_human", "human_active")
TERMINAL_STATES = ("completed", "cancelled", "expired")
ALLOWED_REASONS = (
    "sensitive_input",
    "payment",
    "login",
    "captcha",
    "identity_check",
    "operator_requested",
    "other",
)


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


class HumanTakeoverStore:
    """SQLite-backed takeover coordinator.

    One open takeover is allowed at a time for a single-operator Conveyor
    installation. SQLite provides cross-process coordination and restart
    persistence without putting any sensitive UI contents in the database.
    """

    def __init__(self, settings: Any) -> None:
        root = Path(settings.codex_memory_root) / "state"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "human_takeover.sqlite3"
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
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS human_takeovers (
                        id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        task_id TEXT,
                        requested_by TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        activated_at REAL,
                        closed_at REAL,
                        close_reason TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_human_takeovers_state_expiry
                        ON human_takeovers(state, expires_at);
                    """
                )
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _expire_locked(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            """UPDATE human_takeovers
               SET state = 'expired', updated_at = ?, closed_at = ?, close_reason = 'ttl_expired'
               WHERE state IN ('waiting_for_human', 'human_active') AND expires_at <= ?""",
            (now, now, now),
        )

    def start(
        self,
        *,
        reason: str,
        task_id: str | None = None,
        requested_by: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        reason = _safe_text(reason, 64).lower()
        if reason not in ALLOWED_REASONS:
            raise ValueError(f"unsupported takeover reason: {reason}")
        ttl_seconds = int(ttl_seconds)
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ValueError("takeover ttl must be between 30 and 1800 seconds")
        task_id = _safe_text(task_id, 128) or None
        requested_by = _safe_text(requested_by, 128) or None
        now = time.time()
        session_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_locked(conn, now)
            existing = conn.execute(
                "SELECT * FROM human_takeovers WHERE state IN ('waiting_for_human','human_active') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if existing is not None:
                conn.rollback()
                raise RuntimeError("a human takeover session is already open")
            conn.execute(
                """INSERT INTO human_takeovers
                   (id, state, reason, task_id, requested_by, created_at, updated_at, expires_at)
                   VALUES (?, 'waiting_for_human', ?, ?, ?, ?, ?, ?)""",
                (session_id, reason, task_id, requested_by, now, now, now + ttl_seconds),
            )
            conn.commit()
            return self.get(session_id) or {}
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, session_id: str) -> dict[str, Any] | None:
        session_id = _safe_text(session_id, 64)
        if not session_id:
            return None
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                self._expire_locked(conn, now)
            return self._row(conn.execute(
                "SELECT * FROM human_takeovers WHERE id = ?", (session_id,)
            ).fetchone())
        finally:
            conn.close()

    def current(self) -> dict[str, Any] | None:
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                self._expire_locked(conn, now)
            return self._row(conn.execute(
                """SELECT * FROM human_takeovers
                   WHERE state IN ('waiting_for_human','human_active')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone())
        finally:
            conn.close()

    def activate(self, session_id: str) -> dict[str, Any] | None:
        return self._transition(session_id, from_states=("waiting_for_human",), to_state="human_active")

    def extend(self, session_id: str, ttl_seconds: int) -> dict[str, Any] | None:
        """Push an open lease's expiry out to `ttl_seconds` from now.

        Lets a holder that proves it is still present keep a short TTL, so an
        abandoned lease still frees the desktop quickly. Returns None when the
        lease is no longer open.
        """
        session_id = _safe_text(session_id, 64)
        ttl_seconds = int(ttl_seconds)
        if not session_id or ttl_seconds < 30 or ttl_seconds > 1800:
            return None
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_locked(conn, now)
            updated = conn.execute(
                """UPDATE human_takeovers SET expires_at = ?, updated_at = ?
                   WHERE id = ? AND state IN ('waiting_for_human','human_active')""",
                (now + ttl_seconds, now, session_id),
            ).rowcount
            conn.commit()
            if not updated:
                return None
            return self._row(conn.execute(
                "SELECT * FROM human_takeovers WHERE id = ?", (session_id,)
            ).fetchone())
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, session_id: str) -> dict[str, Any] | None:
        return self._transition(
            session_id,
            from_states=OPEN_STATES,
            to_state="completed",
            close_reason="operator_completed",
        )

    def cancel(self, session_id: str) -> dict[str, Any] | None:
        return self._transition(
            session_id,
            from_states=OPEN_STATES,
            to_state="cancelled",
            close_reason="operator_cancelled",
        )

    def _transition(
        self,
        session_id: str,
        *,
        from_states: tuple[str, ...],
        to_state: str,
        close_reason: str | None = None,
    ) -> dict[str, Any] | None:
        session_id = _safe_text(session_id, 64)
        if not session_id:
            return None
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_locked(conn, now)
            row = conn.execute("SELECT * FROM human_takeovers WHERE id = ?", (session_id,)).fetchone()
            if row is None or str(row["state"]) not in from_states:
                conn.rollback()
                return None
            activated_at = now if to_state == "human_active" else row["activated_at"]
            closed_at = now if to_state in TERMINAL_STATES else None
            conn.execute(
                """UPDATE human_takeovers
                   SET state = ?, updated_at = ?, activated_at = ?, closed_at = ?, close_reason = ?
                   WHERE id = ?""",
                (to_state, now, activated_at, closed_at, close_reason, session_id),
            )
            conn.commit()
            return self._row(conn.execute(
                "SELECT * FROM human_takeovers WHERE id = ?", (session_id,)
            ).fetchone())
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def public(session: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return the intentionally small payload safe for Web/chat surfaces."""
        if not session:
            return None
        now = time.time()
        return {
            "id": session.get("id"),
            "state": session.get("state"),
            "reason": session.get("reason"),
            "task_id": session.get("task_id"),
            "requested_by": session.get("requested_by"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "remaining_seconds": max(0, int(float(session.get("expires_at") or 0) - now)),
        }


def takeover_blocks_automation(settings: Any) -> bool:
    """Fail-safe predicate for computer-use callers.

    Future Web/desktop integrations should check this before every mutating
    computer action. An open handoff means the human owns the GUI.
    """
    return HumanTakeoverStore(settings).current() is not None
