"""Read-only Agent screen projections and narrowly scoped task cancellation.

The Web Console bearer login identifies the one canonical WEB_OPERATOR.
Untrusted agent_id never authorizes another operator, archived Agent,
or a forged AgentContext; the mailbox body is *always* data.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import agent_context as ac
import agent_mailbox
import agent_delegation


def enabled(settings):
    return ac.enabled(settings)


def features(settings):
    on = enabled(settings)
    return {"enabled": on, "inbox": on and agent_mailbox.enabled(settings),
            "tasks": on and agent_delegation.enabled(settings)}


def _actor(settings, agent_id, operator="web-console"):
    import agents
    if not enabled(settings) or not isinstance(agent_id, str):
        raise ValueError("agent screens disabled")
    # A request can ONLY claim the console's own canonical operator.
    if operator != agents.WEB_OPERATOR:
        raise PermissionError("operator denied")
    if not agent_id or len(agent_id) > 32:
        raise ValueError("explicit agent_id required")
    bound = ac.current()
    if bound is not None and (bound.operator_identity != operator or
                              bound.origin_channel != "web" or
                              bound.agent_id != agent_id):
        raise PermissionError("bound agent mismatch")
    ctx = ac.resolve(settings, "web", agents.chat_id_for(agent_id), operator)
    if ctx.agent_id != agent_id or ctx.operator_identity != operator:
        raise PermissionError("agent identity denied")
    ac.validated(settings, ctx, "web", agents.chat_id_for(agent_id), operator)
    return ctx


@contextmanager
def _database(settings):
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    db = sqlite3.connect(str(path), timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        yield db
    finally:
        db.close()


def list_items(settings, agent_id, view, *, operator="web-console", limit=50):
    ctx = _actor(settings, agent_id, operator)
    flags = features(settings)
    if (view == "inbox" and not flags["inbox"] or
        view in ("tasks", "audit") and not flags["tasks"] or
        view not in ("inbox", "tasks", "audit")):
        raise ValueError("screen unavailable")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    if view == "audit":
        import agent_security
        return agent_security.audit_for_agent(
            settings, ctx.agent_id, ctx.operator_identity, limit)
    with _database(settings) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_mailbox'").fetchone():
            return []
        if view == "inbox":
            rows = db.execute(
                """SELECT id,from_agent,to_agent,operator,kind,body,status,
                          created_at,deadline
                   FROM agent_mailbox
                   WHERE to_agent=? AND operator=?
                     AND kind IN ('message','task_request','task_result')
                   ORDER BY created_at DESC,id DESC LIMIT ?""",
                (ctx.agent_id, ctx.operator_identity, limit)).fetchall()
            return [{**dict(row), "untrusted": True,
                     "instruction_authority": False,
                     "approval_authority": False} for row in rows]
        rows = db.execute(
            """SELECT id,from_agent,to_agent,operator,status,attempts,
                      max_attempts,created_at,deadline,lease_expires_at
               FROM agent_mailbox
               WHERE operator=? AND kind='task'
                 AND (from_agent=? OR to_agent=?)
               ORDER BY created_at DESC,id DESC LIMIT ?""",
            (ctx.operator_identity, ctx.agent_id, ctx.agent_id, limit)).fetchall()
        return [dict(row) for row in rows]


def cancel_task(settings, agent_id, task_id, *, operator="web-console"):
    """Cancel one task only, with a write-lock check of its agent/operator."""
    ctx = _actor(settings, agent_id, operator)
    if not features(settings)["tasks"] or not isinstance(task_id, str):
        raise ValueError("tasks unavailable")
    if len(task_id) != 32 or any(ch not in "0123456789abcdef" for ch in task_id):
        raise ValueError("invalid task id")
    import agent_mailbox_db as dbmod
    with dbmod.transaction(settings) as db:
        row = db.execute(
            """SELECT id,status FROM agent_mailbox
               WHERE id=? AND operator=? AND kind='task'
               AND (from_agent=? OR to_agent=?)""",
            (task_id, ctx.operator_identity, ctx.agent_id, ctx.agent_id)).fetchone()
        if row is None:
            raise LookupError("task not found")
        if row["status"] not in ("queued", "leased", "failed"):
            return False
        db.execute(
            """UPDATE agent_mailbox SET status='dead',lease_owner=NULL,
               lease_expires_at=NULL
               WHERE id=? AND operator=? AND kind='task'
               AND status IN ('queued','leased','failed')""",
            (task_id, ctx.operator_identity))
    import agent_security
    agent_security.audit(settings, operator=ctx.operator_identity,
        event="cancel", agent_id=ctx.agent_id, reason="task_cancelled")
    # The worker may run on the Web Console's event loop, not this request
    # thread. Schedule cancellation there instead of touching the loop here.
    import agent_delegation_worker as worker
    task = worker._running.get((ctx.operator_identity, task_id))
    if task is not None and not task.done():
        try:
            task.get_loop().call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # Lease already fenced in the database; result cannot commit.
    return True
