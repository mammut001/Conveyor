"""Structured, SQLite-backed session transcript store.

This complements the legacy JSONL context cache in ``handlers/session.py``.
The JSONL file remains the prompt-injection source for backward compatibility;
this store is the durable UI/domain model for browser and future clients.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conveyor.core.redaction import redact_text, truncate
from security.secrets import is_sensitive_key

logger = logging.getLogger("transcript_store")

_MAX_CONTENT = 16_000
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}
_MAX_METADATA_TEXT = 2_000
_BACKFILL_CHUNK_SIZE = 200
_SNIPPET_MAX_CHARS = 200
_FTS5_AVAILABLE: bool = True
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_supervisor_worktree_session(
    session_id: str | None,
    source_chat_id: str | None,
    session_channel: str | None,
) -> bool:
    sid = str(session_id or "")
    chat_id = str(source_chat_id or "")
    ch = str(session_channel or "")
    return (
        sid.startswith("supwt-")
        or ":supwt-" in sid
        or chat_id.startswith("supwt-")
        or ch.startswith("supwt")
        or ch == "supervisor"
    )


def _extract_operator_id(session_id: str | None, row_operator_id: str | None) -> str | None:
    if row_operator_id:
        return str(row_operator_id)
    parts = str(session_id or "").split(":", 2)
    if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
        return parts[1]
    return None


def _make_snippet(content: str, query: str, terms: list[str], max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    clean = re.sub(r"\s+", " ", content or "").strip()
    if len(clean) <= max_chars:
        return clean
    low = clean.lower()
    needle = (query or "").strip().lower()
    hit_pos = low.find(needle) if needle else -1
    hit_len = len(needle) if hit_pos >= 0 else 0
    if hit_pos < 0:
        for term in terms:
            t_low = term.lower()
            if not t_low:
                continue
            pos = low.find(t_low)
            if pos >= 0:
                hit_pos = pos
                hit_len = len(t_low)
                break
    if hit_pos < 0:
        return clean[: max_chars - 1].rstrip() + "…"
    center = hit_pos + hit_len // 2
    window = max_chars - 2
    start = max(0, center - window // 2)
    end = min(len(clean), start + window)
    if end == len(clean):
        start = max(0, end - (max_chars - 1 if start > 0 else max_chars))
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    budget = max_chars - len(prefix) - len(suffix)
    excerpt = clean[start : start + budget]
    return (prefix + excerpt + suffix)[:max_chars]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def session_identity(channel: str, chat_id: str, operator_id: str) -> str:
    """Return a stable identity without allowing cross-channel collisions."""
    if not channel or not chat_id or not operator_id:
        raise ValueError("channel, chat_id and operator_id are required")
    return f"{channel}:{operator_id}:{chat_id}"


def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            name = str(key)[:128]
            lowered = name.lower()
            if is_sensitive_key(name) or "reasoning" in lowered or "thinking" in lowered:
                result[name] = "[REDACTED]"
            else:
                result[name] = _safe_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, depth=depth + 1) for item in list(value)[:32]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return truncate(redact_text(str(value)), _MAX_METADATA_TEXT)


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
        self._fts_available: bool = _FTS5_AVAILABLE
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA secure_delete = ON")
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
                        source_chat_id TEXT,
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
            if not _FTS5_AVAILABLE or not self._fts_available:
                self._fts_available = False
                return
            try:
                with conn:
                    conn.executescript(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS session_messages_fts USING fts5(
                            content,
                            session_id UNINDEXED,
                            message_id UNINDEXED
                        );
                        CREATE TRIGGER IF NOT EXISTS session_messages_fts_ai
                        AFTER INSERT ON session_messages BEGIN
                            INSERT INTO session_messages_fts(rowid, content, session_id, message_id)
                            VALUES (new.rowid, new.content, new.session_id, new.id);
                        END;
                        CREATE TRIGGER IF NOT EXISTS session_messages_fts_ad
                        AFTER DELETE ON session_messages BEGIN
                            DELETE FROM session_messages_fts WHERE rowid = old.rowid;
                        END;
                        CREATE TRIGGER IF NOT EXISTS session_messages_fts_au
                        AFTER UPDATE OF content, session_id, id ON session_messages BEGIN
                            DELETE FROM session_messages_fts WHERE rowid = old.rowid;
                            INSERT INTO session_messages_fts(rowid, content, session_id, message_id)
                            VALUES (new.rowid, new.content, new.session_id, new.id);
                        END;
                        """
                    )
                self._fts_available = True
                self._backfill_fts(conn)
            except sqlite3.OperationalError:
                self._fts_available = False
                logger.debug("SQLite FTS5 unavailable for session_messages; falling back to LIKE")
        finally:
            conn.close()

    def _backfill_fts(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        chunk_size: int = _BACKFILL_CHUNK_SIZE,
    ) -> int:
        """Idempotently backfill pre-existing session_messages into session_messages_fts in chunks."""
        if not self._fts_available or not _FTS5_AVAILABLE:
            return 0
        own_conn = conn is None
        db = self._connect() if own_conn else conn
        assert db is not None
        total = 0
        batch_limit = max(1, int(chunk_size))
        try:
            msg_count = int(db.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0])
            if msg_count == 0:
                return 0
            fts_count = int(db.execute("SELECT COUNT(*) FROM session_messages_fts").fetchone()[0])
            if fts_count >= msg_count:
                missing_one = db.execute(
                    """SELECT 1 FROM session_messages m
                       LEFT JOIN session_messages_fts f ON f.rowid = m.rowid
                       WHERE f.rowid IS NULL LIMIT 1"""
                ).fetchone()
                if missing_one is None:
                    return 0
            while True:
                rows = db.execute(
                    """SELECT m.rowid, m.content, m.session_id, m.id
                       FROM session_messages m
                       LEFT JOIN session_messages_fts f ON f.rowid = m.rowid
                       WHERE f.rowid IS NULL
                       ORDER BY m.rowid ASC
                       LIMIT ?""",
                    (batch_limit,),
                ).fetchall()
                if not rows:
                    break
                with db:
                    db.executemany(
                        """INSERT INTO session_messages_fts(rowid, content, session_id, message_id)
                           VALUES (?, ?, ?, ?)""",
                        [(r[0], r[1], r[2], r[3]) for r in rows],
                    )
                total += len(rows)
            return total
        except sqlite3.OperationalError:
            self._fts_available = False
            return total
        finally:
            if own_conn:
                db.close()

    def ensure_session(
        self,
        session_id: str,
        *,
        channel: str | None = None,
        operator_id: str | None = None,
        source_chat_id: str | None = None,
        title: str | None = None,
        touch: bool = True,
        create_only: bool = False,
        insert_if_absent: bool = False,
    ) -> dict[str, Any]:
        if not session_id:
            raise ValueError("session_id is required")
        now = _utc_now()
        safe_title = truncate(redact_text(title or "New session"), 120)
        do_touch = bool(touch and not create_only and not insert_if_absent)
        conn = self._connect()
        try:
            with conn:
                if do_touch:
                    conn.execute(
                        """INSERT INTO sessions
                           (id, channel, operator_id, source_chat_id, title, created_at, updated_at, archived)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                           ON CONFLICT(id) DO UPDATE SET
                             channel = COALESCE(excluded.channel, sessions.channel),
                             operator_id = COALESCE(excluded.operator_id, sessions.operator_id),
                             source_chat_id = COALESCE(excluded.source_chat_id, sessions.source_chat_id),
                             archived = 0,
                             updated_at = excluded.updated_at""",
                        (session_id, channel, operator_id, source_chat_id, safe_title, now, now),
                    )
                else:
                    conn.execute(
                        """INSERT INTO sessions
                           (id, channel, operator_id, source_chat_id, title, created_at, updated_at, archived)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                           ON CONFLICT(id) DO NOTHING""",
                        (session_id, channel, operator_id, source_chat_id, safe_title, now, now),
                    )
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def insert_session_if_absent(
        self,
        session_id: str,
        *,
        channel: str | None = None,
        operator_id: str | None = None,
        source_chat_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self.ensure_session(
            session_id,
            channel=channel,
            operator_id=operator_id,
            source_chat_id=source_chat_id,
            title=title,
            touch=False,
            create_only=True,
            insert_if_absent=True,
        )

    def ensure_session_if_missing(
        self,
        session_id: str,
        *,
        channel: str | None = None,
        operator_id: str | None = None,
        source_chat_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self.insert_session_if_absent(
            session_id,
            channel=channel,
            operator_id=operator_id,
            source_chat_id=source_chat_id,
            title=title,
        )

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
        source_chat_id: str | None = None,
    ) -> TranscriptMessage:
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"unsupported transcript role: {role}")
        safe_content = truncate(redact_text(content or ""), _MAX_CONTENT)
        self.ensure_session(
            session_id,
            channel=channel,
            operator_id=operator_id,
            source_chat_id=source_chat_id,
            title=safe_content[:80] if role == "user" and safe_content else None,
        )
        provenance = dict(metadata or {})
        if channel in ("web", "telegram", "feishu"):
            provenance.setdefault("origin_channel", channel)
        message = TranscriptMessage(
            id=uuid.uuid4().hex,
            session_id=session_id,
            role=role,
            content=safe_content,
            created_at=_utc_now(),
            job_id=job_id,
            kind=kind,
            metadata=_safe_metadata(provenance),
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
                           (SELECT COUNT(*) FROM queued_jobs q
                            WHERE q.chat_id = COALESCE(s.source_chat_id, s.id)
                              AND (s.channel IS NULL OR q.channel = s.channel)
                              AND (s.operator_id IS NULL OR q.operator_id = s.operator_id)) AS job_count,
                           (SELECT m.kind FROM session_messages m
                            WHERE m.session_id = s.id
                            ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS last_message_kind
                    FROM sessions s {where}
                    ORDER BY updated_at DESC LIMIT ?""",
                (min(max(1, int(limit)), 200),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def last_message(self, session_id: str) -> dict[str, Any] | None:
        """Newest message of a session (role, content, created_at), or None."""
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT role, content, created_at FROM session_messages
                   WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None
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

    def archive_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (session_id,))
                return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
                if self._fts_available and _FTS5_AVAILABLE:
                    try:
                        conn.execute("DELETE FROM session_messages_fts WHERE session_id = ?", (session_id,))
                        conn.execute("INSERT INTO session_messages_fts(session_messages_fts) VALUES('optimize')")
                    except sqlite3.OperationalError:
                        pass
                cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def _session_matches_filters(
        row: sqlite3.Row,
        *,
        channel: str | None,
        include_archived: bool,
        operator_id: str | set[str | None] | tuple[str | None, ...] | frozenset[str | None] | None,
    ) -> bool:
        if not include_archived and int(row["archived"] or 0) != 0:
            return False
        session_id = str(row["session_id"] or "")
        source_chat_id = row["source_chat_id"]
        row_channel = row["channel"]
        is_supwt = _is_supervisor_worktree_session(session_id, source_chat_id, row_channel)
        req_channel = (channel or "").strip() or None
        wants_supwt = bool(req_channel and (req_channel.startswith("supwt") or req_channel == "supervisor"))
        if not wants_supwt:
            if is_supwt:
                return False
            if req_channel is not None:
                if row_channel != req_channel and not (row_channel is None and session_id.startswith(f"{req_channel}:")):
                    return False
        else:
            if not is_supwt:
                return False
            if req_channel not in ("supwt", "supwt-", "supervisor"):
                if (
                    row_channel != req_channel
                    and source_chat_id != req_channel
                    and session_id != req_channel
                    and not session_id.endswith(f":{req_channel}")
                ):
                    return False

        row_op = _extract_operator_id(session_id, row["operator_id"])
        if operator_id is None:
            if row_op and row_op.startswith("import:"):
                return False
            return True
        if isinstance(operator_id, str):
            return row_op == operator_id
        return row_op in operator_id

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        channel: str | None = None,
        include_archived: bool = False,
        operator_id: str | set[str | None] | tuple[str | None, ...] | frozenset[str | None] | None = None,
    ) -> list[dict[str, Any]]:
        needle = (query or "").strip()[:200]
        if not needle:
            return []
        capped_limit = min(max(1, int(limit)), 200)
        ws_tokens = [t for t in re.split(r"\s+", needle) if t]
        fts_tokens = [t for t in re.findall(r"\w+", needle) if t]
        from personal_tools.long_term_memory import search_terms
        extra_terms = search_terms(needle)
        all_terms = list(dict.fromkeys([*ws_tokens, *extra_terms]))[:16] or [needle]
        has_cjk = _has_cjk(needle)
        use_fts = bool(self._fts_available and _FTS5_AVAILABLE and not has_cjk and fts_tokens)

        candidates: dict[str, tuple[sqlite3.Row, float]] = {}
        fetch_cap = max(60, capped_limit * 6)
        conn = self._connect()
        try:
            if use_fts:
                try:
                    fts_expr = " OR ".join('"' + tok.replace('"', '""') + '"' for tok in fts_tokens[:16])
                    rows = conn.execute(
                        """SELECT m.id AS message_id,
                                  m.session_id AS session_id,
                                  m.role AS role,
                                  m.content AS content,
                                  m.created_at AS created_at,
                                  COALESCE(s.title, 'New session') AS title,
                                  s.channel AS channel,
                                  COALESCE(s.archived, 0) AS archived,
                                  s.operator_id AS operator_id,
                                  s.source_chat_id AS source_chat_id,
                                  bm25(session_messages_fts) AS raw_bm25
                           FROM session_messages_fts f
                           JOIN session_messages m ON m.rowid = f.rowid
                           LEFT JOIN sessions s ON s.id = m.session_id
                           WHERE session_messages_fts MATCH ?
                           ORDER BY bm25(session_messages_fts) ASC, m.created_at DESC
                           LIMIT ?""",
                        (fts_expr, fetch_cap),
                    ).fetchall()
                    for r in rows:
                        if self._session_matches_filters(
                            r,
                            channel=channel,
                            include_archived=include_archived,
                            operator_id=operator_id,
                        ):
                            raw_bm25 = float(r["raw_bm25"] or 0.0)
                            candidates[str(r["message_id"])] = (r, max(0.001, -raw_bm25))
                except sqlite3.OperationalError:
                    use_fts = False

            if not use_fts or not candidates:
                like_tokens = ws_tokens or [needle]
                predicates = " OR ".join("LOWER(m.content) LIKE LOWER(?) ESCAPE '\\'" for _ in like_tokens)
                like_args = [f"%{_escape_like(t)}%" for t in like_tokens]
                rows = conn.execute(
                    f"""SELECT m.id AS message_id,
                               m.session_id AS session_id,
                               m.role AS role,
                               m.content AS content,
                               m.created_at AS created_at,
                               COALESCE(s.title, 'New session') AS title,
                               s.channel AS channel,
                               COALESCE(s.archived, 0) AS archived,
                               s.operator_id AS operator_id,
                               s.source_chat_id AS source_chat_id
                        FROM session_messages m
                        LEFT JOIN sessions s ON s.id = m.session_id
                        WHERE ({predicates})
                        ORDER BY m.created_at DESC, m.id DESC
                        LIMIT ?""",
                    [*like_args, fetch_cap],
                ).fetchall()
                for r in rows:
                    if self._session_matches_filters(
                        r,
                        channel=channel,
                        include_archived=include_archived,
                        operator_id=operator_id,
                    ):
                        candidates.setdefault(str(r["message_id"]), (r, 0.0))

                if not candidates and has_cjk and extra_terms:
                    cjk_terms = extra_terms[:12]
                    predicates = " OR ".join("LOWER(m.content) LIKE LOWER(?) ESCAPE '\\'" for _ in cjk_terms)
                    like_args = [f"%{_escape_like(t)}%" for t in cjk_terms]
                    rows = conn.execute(
                        f"""SELECT m.id AS message_id,
                                   m.session_id AS session_id,
                                   m.role AS role,
                                   m.content AS content,
                                   m.created_at AS created_at,
                                   COALESCE(s.title, 'New session') AS title,
                                   s.channel AS channel,
                                   COALESCE(s.archived, 0) AS archived,
                                   s.operator_id AS operator_id,
                                   s.source_chat_id AS source_chat_id
                            FROM session_messages m
                            LEFT JOIN sessions s ON s.id = m.session_id
                            WHERE ({predicates})
                            ORDER BY m.created_at DESC, m.id DESC
                            LIMIT ?""",
                        [*like_args, fetch_cap],
                    ).fetchall()
                    min_hits = 1 if len(cjk_terms) == 1 else 2
                    for r in rows:
                        if not self._session_matches_filters(
                            r,
                            channel=channel,
                            include_archived=include_archived,
                            operator_id=operator_id,
                        ):
                            continue
                        low_text = str(r["content"] or "").lower()
                        if sum(1 for t in cjk_terms if t.lower() in low_text) >= min_hits:
                            candidates.setdefault(str(r["message_id"]), (r, 0.0))
        finally:
            conn.close()

        low_needle = needle.lower()
        results: list[dict[str, Any]] = []
        for row, fts_score in candidates.values():
            content = str(row["content"] or "")
            low_content = content.lower()
            exact_boost = 10.0 if low_needle in low_content else 0.0
            token_hits = sum(2.0 for t in ws_tokens if t.lower() in low_content)
            term_hits = sum(1.0 for t in all_terms if t.lower() in low_content)
            score = round(exact_boost + token_hits + term_hits + fts_score, 6)
            if score <= 0.0:
                score = 0.001
            channel_val = row["channel"]
            if channel_val is None:
                parts = str(row["session_id"] or "").split(":", 2)
                if len(parts) == 3 and parts[0]:
                    channel_val = parts[0]
            results.append(
                {
                    "session_id": str(row["session_id"]),
                    "title": str(row["title"] or "New session"),
                    "channel": channel_val,
                    "created_at": str(row["created_at"]),
                    "message_id": str(row["message_id"]),
                    "role": str(row["role"]),
                    "snippet": _make_snippet(content, needle, all_terms, _SNIPPET_MAX_CHARS),
                    "score": score,
                }
            )
        results.sort(
            key=lambda item: (item["score"], item["created_at"], item["message_id"]),
            reverse=True,
        )
        return results[:capped_limit]

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

    @staticmethod
    def _apply_kind_filter(
        sql: str,
        params: list[Any],
        *,
        kinds: tuple[str | None, ...] | list[str | None] | set[str | None] | None = None,
        exclude_kinds: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> str:
        if kinds is not None:
            kinds_list = list(kinds)
            include_null = None in kinds_list
            non_null_kinds = [k for k in kinds_list if k is not None]
            if include_null and non_null_kinds:
                placeholders = ",".join("?" for _ in non_null_kinds)
                sql += f" AND (kind IN ({placeholders}) OR kind IS NULL)"
                params.extend(non_null_kinds)
            elif include_null:
                sql += " AND kind IS NULL"
            elif non_null_kinds:
                placeholders = ",".join("?" for _ in non_null_kinds)
                sql += f" AND kind IN ({placeholders})"
                params.extend(non_null_kinds)
            else:
                sql += " AND 1=0"
        if exclude_kinds is not None:
            ex_list = list(exclude_kinds)
            include_null = None in ex_list
            non_null_ex = [k for k in ex_list if k is not None]
            if non_null_ex:
                placeholders = ",".join("?" for _ in non_null_ex)
                sql += f" AND (kind IS NULL OR kind NOT IN ({placeholders}))"
                params.extend(non_null_ex)
            if include_null:
                sql += " AND kind IS NOT NULL"
        return sql

    def recent_messages(
        self,
        session_id: str,
        *,
        limit: int = 50,
        roles: tuple[str, ...] | list[str] | set[str] | None = None,
        kinds: tuple[str | None, ...] | list[str | None] | set[str | None] | None = None,
        exclude_kinds: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest messages for a session in chronological order (ASC).

        Queries descending by created_at to efficiently capture the latest-N
        messages even in very long sessions, then returns them in chronological order.
        """
        conn = self._connect()
        try:
            params: list[Any] = [session_id]
            sql = "SELECT * FROM session_messages WHERE session_id = ?"
            if roles:
                role_list = list(roles)
                placeholders = ",".join("?" for _ in role_list)
                sql += f" AND role IN ({placeholders})"
                params.extend(role_list)
            sql = self._apply_kind_filter(sql, params, kinds=kinds, exclude_kinds=exclude_kinds)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(min(max(1, int(limit)), 2_000))
            rows = conn.execute(sql, params).fetchall()
            return [self._message_dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def count_messages(
        self,
        session_id: str,
        *,
        roles: tuple[str, ...] | list[str] | set[str] | None = None,
        kinds: tuple[str | None, ...] | list[str | None] | set[str | None] | None = None,
        exclude_kinds: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> int:
        """Count total messages for a session, optionally filtered by roles and kinds."""
        conn = self._connect()
        try:
            params: list[Any] = [session_id]
            sql = "SELECT COUNT(*) FROM session_messages WHERE session_id = ?"
            if roles:
                role_list = list(roles)
                placeholders = ",".join("?" for _ in role_list)
                sql += f" AND role IN ({placeholders})"
                params.extend(role_list)
            sql = self._apply_kind_filter(sql, params, kinds=kinds, exclude_kinds=exclude_kinds)
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0
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
        source_chat_id: str | None = None,
        kind: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[TranscriptMessage, TranscriptMessage]:
        user = self.append(
            session_id,
            "user",
            user_text,
            job_id=job_id,
            kind=kind,
            metadata=metadata,
            channel=channel,
            operator_id=operator_id,
            source_chat_id=source_chat_id,
        )
        assistant = self.append(
            session_id,
            "assistant",
            assistant_text,
            job_id=job_id,
            kind=kind,
            metadata=metadata,
            channel=channel,
            operator_id=operator_id,
            source_chat_id=source_chat_id,
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
