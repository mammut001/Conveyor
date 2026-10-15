"""Transactional v2 mailbox kind migration, exclusively with delegation on.

Retains every PR2/3 row and its parent links; no schema mutation flag off.
Caller owns BEGIN IMMEDIATE and has foreign_keys temporarily disabled.
"""
from __future__ import annotations

import sqlite3


def ensure(conn: sqlite3.Connection) -> None:
    import agent_mailbox_db
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_mailbox'"
    ).fetchone()
    if row is None:
        raise RuntimeError("mailbox table missing")
    if "'task','result'" in row[0]:
        return
    # Rebuild to widen the existing SQLite CHECK on kind; ALTER cannot
    # modify a CHECK. Preserve all columns, indexes via rebuild and FK links.
    sql = agent_mailbox_db._SCHEMA
    upgraded = sql.replace(
        "'message','task_request','task_result'",
        "'message','task_request','task_result','task','result'")
    upgraded = upgraded.replace(
        "CREATE TABLE IF NOT EXISTS agent_mailbox (",
        "CREATE TABLE agent_mailbox_upgrade ("
    ).replace(
        "REFERENCES agent_mailbox(id)",
        "REFERENCES agent_mailbox_upgrade(id)"
    )
    conn.execute(upgraded)
    columns = (
        "id,from_agent,to_agent,operator,kind,body,idempotency_key,status,"
        "lease_owner,lease_expires_at,attempts,max_attempts,deadline,created_at,"
        "parent_message_id,depth"
    )
    conn.execute(
        f"INSERT INTO agent_mailbox_upgrade ({columns}) "
        f"SELECT {columns} FROM agent_mailbox"
    )
    conn.execute("DROP TABLE agent_mailbox")
    conn.execute("ALTER TABLE agent_mailbox_upgrade RENAME TO agent_mailbox")
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("mailbox foreign-key migration failed")
