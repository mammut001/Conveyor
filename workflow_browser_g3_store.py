"""G3 durable one-shot previews. SQLite is the authority, not chat/relay state."""
from __future__ import annotations
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing

from personal_tools.browser_read import _db, _operator_key
from workflow_browser_external_identity import ExternalPolicyDenied
from workflow_browser_g3_recovery import owner, recover

TTL = 300

def _conn(settings):
    conn = sqlite3.connect(_db(settings), timeout=5, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS browser_g3_submits (
        token TEXT PRIMARY KEY, operator_key TEXT NOT NULL,
        source_url TEXT NOT NULL, form_ref INTEGER NOT NULL,
        fields_json TEXT NOT NULL, preview_json TEXT NOT NULL,
        preview_hash TEXT NOT NULL, screenshot BLOB NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL,
        state TEXT NOT NULL
    )""")
    recover(conn)
    return conn

def digest(preview):
    return hashlib.sha256(json.dumps(preview, sort_keys=True,
        ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()

def save(settings, *, operator_id, url, form_ref, fields, preview, screenshot):
    now = time.time()
    token = secrets.token_hex(16)
    if len(screenshot) > 400_000:
        raise ExternalPolicyDenied("g3_screenshot_too_large")
    with closing(_conn(settings)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO browser_g3_submits
            (token,operator_key,source_url,form_ref,fields_json,preview_json,
             preview_hash,screenshot,created_at,expires_at,state) VALUES
            (?,?,?,?,?,?,?,?,?,?,?)""", (
            token, _operator_key(operator_id), url, form_ref,
            json.dumps(fields, sort_keys=True), json.dumps(preview, sort_keys=True),
            digest(preview), screenshot, now, now + TTL, "pending"))
        conn.commit()
    return token, digest(preview)

def review(settings, token):
    if not isinstance(token,str) or len(token)!=32:
        return None
    with closing(_conn(settings)) as conn:
        row=conn.execute("""SELECT preview_json,preview_hash,screenshot,expires_at,state
                FROM browser_g3_submits WHERE token=?""",(token,)).fetchone()
    if row is None or row[4] != "pending" or row[3] <= time.time():
        return None
    return {"preview":json.loads(row[0]), "hash":row[1],
            "screenshot":row[2], "expires_at":row[3]}

def claim(settings, token, operator_id, expected_hash):
    """Single atomic claim after an authenticated human approves exact hash."""
    with closing(_conn(settings)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("""SELECT source_url,form_ref,fields_json,preview_json,
                       preview_hash FROM browser_g3_submits
                       WHERE token=? AND operator_key=? AND preview_hash=?
                       AND state='pending' AND expires_at>?""",
                       (token,_operator_key(operator_id),expected_hash,time.time())).fetchone()
        if row is None:
            conn.rollback()
            raise ExternalPolicyDenied("g3_approval_expired_or_used")
        changed=conn.execute("""UPDATE browser_g3_submits SET state='verifying'
                  WHERE token=? AND state='pending'""",(token,)).rowcount
        if changed!=1:
            conn.rollback()
            raise ExternalPolicyDenied("g3_approval_reused")
        conn.commit()
    return row[0],row[1],json.loads(row[2]),json.loads(row[3])

def advance(settings, token, old, new):
    with closing(_conn(settings)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        count=conn.execute("""UPDATE browser_g3_submits
                SET state=?, dispatch_owner=CASE WHEN ?='dispatched'
                    THEN ? ELSE dispatch_owner END
                WHERE token=? AND state=?""",(new,new,owner(),token,old)).rowcount
        if count!=1:
            conn.rollback()
            raise ExternalPolicyDenied("g3_state_conflict")
        conn.commit()

def list_reconciliation(settings, limit=50):
    """Read-only uncertain G3 attempts, including recovered process crashes."""
    with closing(_conn(settings)) as conn:
        rows=conn.execute("""SELECT token,preview_json,preview_hash,screenshot,created_at
            FROM browser_g3_submits WHERE state='needs_reconciliation'
            ORDER BY created_at DESC LIMIT ?""",(limit,)).fetchall()
    return [{"token":t,"preview":json.loads(p),"hash":h,
             "screenshot":s,"created_at":ca} for t,p,h,s,ca in rows]


def state(settings, token):
    with closing(_conn(settings)) as conn:
        row=conn.execute("SELECT state FROM browser_g3_submits WHERE token=?",(token,)).fetchone()
    return row[0] if row else None
