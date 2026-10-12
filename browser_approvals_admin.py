"""Web browser grants and egress: redacted, authenticated WebConsoleHandler backend.

Do not mount these handlers before WebConsoleHandler._require_auth. There is
no arbitrary approval-creation endpoint: creation remains a confirmation flow.
"""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from personal_tools.browser_read import _connect, _db, _requested_url
from personal_tools.browser_approval_scope import _tables
from workflow_browser_egress import EgressAudit
from workflow_browser_external_identity import ExternalPolicyDenied


def _settings(handler):
    settings = getattr(handler.server.control, "settings", None)
    if settings is None:
        from config import load_runtime_settings
        settings = load_runtime_settings()
    return settings


def _id(key, url, kind, field=""):
    return hashlib.sha256(
        (key + "\0" + url + "\0" + kind + "\0" + field).encode()
    ).hexdigest()[:32]


def approvals(settings):
    """Only unexpired grants; expose a pseudonymous approver, not raw IDs."""
    now = time.time()
    with closing(_connect(settings)) as conn:
        _tables(conn)
        paths = conn.execute("""SELECT operator_key,url,expires_at,granted_at
            FROM browser_agent_grants WHERE expires_at>? ORDER BY expires_at""",
            (now,)).fetchall()
        actions = {(k,u): (json.loads(a), t) for k,u,a,t in
            conn.execute("""SELECT operator_key,url,action_types,expires_at
                FROM browser_g2_action_grants WHERE expires_at>?""",(now,))}
        queries = {(k,u,f): t for k,u,f,t in conn.execute("""
            SELECT operator_key,url,field,expires_at FROM browser_g2_query_grants
            WHERE expires_at>?""",(now,))}
    result = []
    for key,url,expiry,granted in paths:
        try:
            host,port,path,_ = _requested_url(url)
        except ExternalPolicyDenied:
            continue
        common = {"host":host,"port":port,"path":path,
                  "approver":"operator:"+key[:12],
                  "operator_fingerprint":key[:12],
                  "url":url}
        result.append(dict(common,id=_id(key,url,"path"),kind="path",
                           query_field=None,expires_at=expiry,granted_at=granted,
                           action_types=[]))
        if (key,url) in actions:
            kinds, t = actions[(key,url)]
            result.append(dict(common,id=_id(key,url,"actions"),kind="actions",
                               query_field=None,expires_at=min(expiry,t),
                               granted_at=granted,action_types=kinds))
        for (qkey,qu,field), t in queries.items():
            if qkey == key and qu == url:
                result.append(dict(common,id=_id(key,url,"query",field),
                                   kind="query",query_field=field,
                                   expires_at=min(expiry,t),granted_at=granted,
                                   action_types=["search_get"]))
    return result


def revoke(settings, approval_id):
    """Revoke exactly one visible grant. Never accept raw paths for deletion."""
    if not isinstance(approval_id,str) or len(approval_id)!=32 or any(
        c not in "0123456789abcdef" for c in approval_id
    ):
        return False
    with closing(_connect(settings)) as conn:
        _tables(conn)
        now = time.time()
        paths = conn.execute("""SELECT operator_key,url FROM browser_agent_grants
            WHERE expires_at>?""",(now,)).fetchall()
        for key,url in paths:
            if _id(key,url,"path") == approval_id:
                conn.execute("DELETE FROM browser_agent_grants WHERE operator_key=? AND url=?",(key,url))
                conn.execute("DELETE FROM browser_g2_action_grants WHERE operator_key=? AND url=?",(key,url))
                conn.execute("DELETE FROM browser_g2_query_grants WHERE operator_key=? AND url=?",(key,url))
                conn.commit()
                return True
            if _id(key,url,"actions") == approval_id:
                conn.execute("DELETE FROM browser_g2_action_grants WHERE operator_key=? AND url=?",(key,url))
                conn.commit()
                return True
            fields=conn.execute("""SELECT field FROM browser_g2_query_grants
                WHERE operator_key=? AND url=? AND expires_at>?""",(key,url,now)).fetchall()
            for (field,) in fields:
                if _id(key,url,"query",field)==approval_id:
                    conn.execute("""DELETE FROM browser_g2_query_grants
                        WHERE operator_key=? AND url=? AND field=?""",(key,url,field))
                    conn.commit()
                    return True
    return False


def egress(settings, limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("invalid limit")
    audit = EgressAudit(_db(settings))
    with sqlite3.connect(audit.path, timeout=2) as conn:
        rows=conn.execute("""SELECT timestamp,domain,method,outcome,reason
            FROM browser_external_egress ORDER BY id DESC LIMIT ?""",
            (limit,)).fetchall()
    # The stored G1/G2 egress schema has no URL, query, form value or body.
    return [{"timestamp":t,"host":host,"method":method,
             "outcome":outcome,"reason":reason} for t,host,method,outcome,reason in rows]


def _kill_path(settings):
    root = _db(settings).parent
    configured = os.environ.get("CONVEYOR_BROWSER_EXTERNAL_KILL_FILE")
    target = Path(configured).expanduser().absolute() if configured else root/"browser-external.kill"
    # Never allow web API to unlink/touch arbitrary operator-configured files.
    if target != root/"browser-external.kill":
        raise ExternalPolicyDenied("kill_path_unmanaged")
    return target


def kill_status(settings):
    path = _kill_path(settings)
    if path.is_symlink():
        raise ExternalPolicyDenied("kill_path_unsafe")
    return path.exists()


def set_kill(settings, enabled):
    if type(enabled) is not bool:
        raise ValueError("enabled must be boolean")
    path = _kill_path(settings)
    if path.is_symlink():
        raise ExternalPolicyDenied("kill_path_unsafe")
    if enabled:
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            if not path.is_file():
                raise ExternalPolicyDenied("kill_path_unsafe")
        else:
            os.close(fd)
    elif path.exists():
        metadata=path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or
                metadata.st_uid != os.getuid()):
            raise ExternalPolicyDenied("kill_path_unsafe")
        path.unlink()
    return kill_status(settings)


def get(handler, path, query):
    if not path.startswith("/api/browser/"):
        return False
    settings = _settings(handler)
    if path == "/api/browser/approvals":
        handler._json(200,{"approvals":approvals(settings),
                           "kill_switch":kill_status(settings)})
    elif path == "/api/browser/egress":
        try:
            limit = int((query.get("limit") or ["50"])[0])
            handler._json(200,{"requests":egress(settings,limit)})
        except ValueError:
            handler._json(400,{"error":"invalid limit"})
    else:
        handler._json(404,{"error":"not found"})
    return True


def post(handler, path, body):
    if path != "/api/browser/kill":
        return False
    if set(body)!={"enabled"} or type(body["enabled"]) is not bool:
        handler._json(400,{"error":"expected boolean enabled"})
    else:
        handler._json(200,{"kill_switch":set_kill(_settings(handler),body["enabled"])})
    return True


def delete(handler, path):
    if not path.startswith("/api/browser/"):
        return False
    parts=path.split("/")
    if len(parts)!=5 or parts[:4]!=["","api","browser","approvals"]:
        handler._json(404,{"error":"not found"})
        return True
    removed = revoke(_settings(handler),parts[4])
    handler._json(200 if removed else 404, {"ok":removed})
    return True
