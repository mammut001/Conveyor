"""G2 operator consent: exact start URL + allowed action kinds + GET search path/field.

This is trusted server-side logic. Model arguments request a *review*, never
grant authorization. Queries themselves are never persisted in grants.
"""
from __future__ import annotations
from contextlib import closing
import json
import time

from personal_tools.browser_read import _connect, _operator_key, _requested_url
from workflow_browser_external_identity import ExternalPolicyDenied, canonical_url
from workflow_browser_g2 import FIELD_RE

ACTION_TTL = 24 * 3600
QUERY_TTL = 3600


def _tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS browser_g2_action_grants (
        operator_key TEXT NOT NULL, url TEXT NOT NULL, action_types TEXT NOT NULL,
        expires_at REAL NOT NULL, PRIMARY KEY(operator_key,url))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS browser_g2_query_grants (
        operator_key TEXT, url TEXT, field TEXT, expires_at REAL,
        PRIMARY KEY(operator_key,url,field))""")


def requested_scope(url, actions):
    """Use path and field as *expected* form identity; browser rechecks DOM."""
    _, _, _, origin = _requested_url(url)
    kinds = tuple(sorted({a["type"] for a in actions}))
    searches = []
    for action in actions:
        if action["type"] != "search_get":
            continue
        path, field = action["path"], action["field"]
        if (not isinstance(path, str) or not isinstance(field, str)
                or not FIELD_RE.fullmatch(field)):
            raise ExternalPolicyDenied("g2_search_scope_denied")
        _requested_url(origin + path)
        searches.append((origin + path, field))
    return kinds, tuple(sorted(set(searches)))


def status(settings, operator_id, url, actions):
    """Compute missing scoped approvals atomically read-only; fail closed."""
    kinds, searches = requested_scope(url, actions)
    key = _operator_key(operator_id)
    now = time.time()
    with closing(_connect(settings)) as conn:
        _tables(conn)
        start = conn.execute("""SELECT expires_at FROM browser_agent_grants
            WHERE operator_key=? AND url=?""", (key, url)).fetchone()
        row = conn.execute("""SELECT action_types,expires_at FROM browser_g2_action_grants
            WHERE operator_key=? AND url=?""", (key, url)).fetchone()
        approved = set(json.loads(row[0])) if row and row[1] > now else set()
        missing = []
        for search_url, field in searches:
            base = conn.execute("""SELECT expires_at FROM browser_agent_grants
                WHERE operator_key=? AND url=?""", (key, search_url)).fetchone()
            query = conn.execute("""SELECT expires_at FROM browser_g2_query_grants
                WHERE operator_key=? AND url=? AND field=?""",
                (key, search_url, field)).fetchone()
            if not base or base[0] <= now or not query or query[0] <= now:
                missing.append((search_url, field))
    return {
        "start_missing": start is None or start[0] <= now,
        "new_actions": sorted(set(kinds) - approved),
        "query_missing": missing,
        "action_types": list(kinds),
    }


def needs_approval(settings, operator_id, url, actions):
    scope = status(settings, operator_id, url, actions)
    return bool(scope["start_missing"] or scope["new_actions"] or scope["query_missing"])


def grant_confirmed(settings, operator_id, url, actions):
    """Called only after authenticated pending confirmation resolved YES."""
    kinds, searches = requested_scope(url, actions)
    key = _operator_key(operator_id)
    now = time.time()
    with closing(_connect(settings)) as conn:
        _tables(conn)
        row = conn.execute("""SELECT action_types,expires_at FROM browser_g2_action_grants
            WHERE operator_key=? AND url=?""", (key,url)).fetchone()
        previous = set(json.loads(row[0])) if row and row[1] > now else set()
        allowed = json.dumps(sorted(previous | set(kinds)))
        conn.execute("""INSERT OR REPLACE INTO browser_agent_grants
            (operator_key,url,expires_at,granted_at) VALUES(?,?,?,?)""",
            (key,url,now+ACTION_TTL,now))
        conn.execute("""INSERT OR REPLACE INTO browser_g2_action_grants
            (operator_key,url,action_types,expires_at) VALUES(?,?,?,?)""",
            (key,url,allowed,now+ACTION_TTL))
        for search_url, field in searches:
            conn.execute("""INSERT OR REPLACE INTO browser_agent_grants
                (operator_key,url,expires_at,granted_at) VALUES(?,?,?,?)""",
                (key,search_url,now+QUERY_TTL,now))
            conn.execute("""INSERT OR REPLACE INTO browser_g2_query_grants
                (operator_key,url,field,expires_at) VALUES(?,?,?,?)""",
                (key,search_url,field,now+QUERY_TTL))
        conn.commit()


def summary(arg):
    """Approval cards show actionable path/field (never the query contents)."""
    from personal_tools.browser_interact import _input
    url, actions = _input(arg)
    scope, searches = requested_scope(url, actions)
    _,_,path,origin = _requested_url(url)
    description = f"G2 GET-only browser start: {origin}{path}; actions: {', '.join(scope) or 'read'}"
    if searches:
        description += "; GET search approvals: " + ", ".join(
            f"{target} [field={field}]" for target, field in searches)
    return description


def assert_live(settings, operator_id, url, actions):
    if needs_approval(settings,operator_id,url,actions):
        raise ExternalPolicyDenied("g2_operator_grant_revoked")


def path_is_live(settings, operator_id, url):
    """Revalidate every destination GET before G2 requests the network."""
    _requested_url(url)
    key = _operator_key(operator_id)
    with closing(_connect(settings)) as conn:
        row = conn.execute("""SELECT expires_at FROM browser_agent_grants
            WHERE operator_key=? AND url=?""", (key, url)).fetchone()
    return row is not None and row[0] > time.time()


def query_is_live(settings, operator_id, url, field):
    key = _operator_key(operator_id)
    with closing(_connect(settings)) as conn:
        _tables(conn)
        rows = conn.execute("""SELECT
            (SELECT expires_at FROM browser_agent_grants
             WHERE operator_key=? AND url=?),
            (SELECT expires_at FROM browser_g2_query_grants
             WHERE operator_key=? AND url=? AND field=?)""",
            (key,url,key,url,field)).fetchone()
    return all(isinstance(v,(int,float)) and v > time.time() for v in rows)
