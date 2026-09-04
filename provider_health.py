"""Persistent provider health and fail-fast circuit-breaker state."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redaction import redact_text, truncate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_provider_error(text: str) -> str:
    value = (text or "").lower()
    if any(token in value for token in ("429", "too many requests", "rate limit", "high demand", "overloaded")):
        return "rate_limited"
    if any(token in value for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication")):
        return "auth_failed"
    if any(token in value for token in ("timed out", "timeout", "connection refused", "connection reset", "dns", "name or service not known", "network is unreachable")):
        return "unreachable"
    return "error"


class ProviderHealthStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.db_path = (Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3").resolve()
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
                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider_id TEXT PRIMARY KEY,
                        config_revision TEXT NOT NULL,
                        status TEXT NOT NULL,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        circuit_open_until REAL NOT NULL DEFAULT 0,
                        last_error_kind TEXT,
                        last_error TEXT,
                        last_error_at TEXT,
                        last_success_at TEXT,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
        finally:
            conn.close()

    def reset(self, provider_id: str) -> None:
        """Forget stale health after an explicit operator config/key change."""
        if not provider_id:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM provider_health WHERE provider_id = ?", (provider_id,))
        finally:
            conn.close()

    def snapshot(self, provider_id: str, config_revision: str) -> dict[str, Any]:
        provider_id = provider_id or "unknown"
        now = time.time()
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM provider_health WHERE provider_id = ?", (provider_id,)).fetchone()
        finally:
            conn.close()
        if row is None or row["config_revision"] != config_revision:
            return {
                "provider_id": provider_id,
                "status": "unknown",
                "circuit_open": False,
                "retry_after_seconds": 0,
                "consecutive_failures": 0,
                "last_error_kind": None,
                "last_error_at": None,
                "last_success_at": None,
            }
        retry_after = max(0, int(float(row["circuit_open_until"] or 0) - now))
        status = str(row["status"] or "unknown")
        if retry_after == 0 and float(row["circuit_open_until"] or 0) > 0 and status != "healthy":
            status = "recovering"
        return {
            "provider_id": provider_id,
            "status": status,
            "circuit_open": retry_after > 0,
            "retry_after_seconds": retry_after,
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "last_error_kind": row["last_error_kind"],
            "last_error_at": row["last_error_at"],
            "last_success_at": row["last_success_at"],
        }

    def can_run(self, provider_id: str, config_revision: str) -> tuple[bool, dict[str, Any]]:
        health = self.snapshot(provider_id, config_revision)
        return (not health["circuit_open"], health)

    def record_success(self, provider_id: str, config_revision: str) -> dict[str, Any]:
        now = _utc_now()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO provider_health
                       (provider_id, config_revision, status, consecutive_failures,
                        circuit_open_until, last_error_kind, last_error,
                        last_error_at, last_success_at, updated_at)
                       VALUES (?, ?, 'healthy', 0, 0, NULL, NULL, NULL, ?, ?)
                       ON CONFLICT(provider_id) DO UPDATE SET
                         config_revision=excluded.config_revision,
                         status='healthy', consecutive_failures=0,
                         circuit_open_until=0, last_error_kind=NULL,
                         last_error=NULL, last_error_at=NULL,
                         last_success_at=excluded.last_success_at,
                         updated_at=excluded.updated_at""",
                    (provider_id, config_revision, now, now),
                )
        finally:
            conn.close()
        return self.snapshot(provider_id, config_revision)

    def record_failure(self, provider_id: str, config_revision: str, error_text: str) -> dict[str, Any]:
        kind = classify_provider_error(error_text)
        threshold_default = int(getattr(self.settings, "conveyor_provider_circuit_threshold", 1))
        cooldown_default = int(getattr(self.settings, "conveyor_provider_circuit_seconds", 180))
        threshold = max(1, int(os.getenv("CONVEYOR_PROVIDER_CIRCUIT_THRESHOLD", str(threshold_default))))
        cooldown = max(1, int(os.getenv("CONVEYOR_PROVIDER_CIRCUIT_SECONDS", str(cooldown_default))))
        current = self.snapshot(provider_id, config_revision)
        failures = current["consecutive_failures"] + 1
        circuit_kind = kind in {"rate_limited", "auth_failed", "unreachable"}
        open_until = time.time() + cooldown if circuit_kind and failures >= threshold else 0
        now = _utc_now()
        safe_error = truncate(redact_text(error_text or kind), 500)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO provider_health
                       (provider_id, config_revision, status, consecutive_failures,
                        circuit_open_until, last_error_kind, last_error,
                        last_error_at, last_success_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                       ON CONFLICT(provider_id) DO UPDATE SET
                         config_revision=excluded.config_revision,
                         status=excluded.status,
                         consecutive_failures=excluded.consecutive_failures,
                         circuit_open_until=excluded.circuit_open_until,
                         last_error_kind=excluded.last_error_kind,
                         last_error=excluded.last_error,
                         last_error_at=excluded.last_error_at,
                         updated_at=excluded.updated_at""",
                    (provider_id, config_revision, kind, failures, open_until, kind, safe_error, now, now),
                )
        finally:
            conn.close()
        return self.snapshot(provider_id, config_revision)


_stores: dict[str, ProviderHealthStore] = {}


def get_provider_health(settings: Any) -> ProviderHealthStore:
    key = str((Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3").resolve())
    store = _stores.get(key)
    if store is None:
        store = ProviderHealthStore(settings)
        _stores[key] = store
    return store
