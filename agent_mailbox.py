"""Durable, opt-in, data-only mailbox for canonical Conveyor agents.

No delivery worker, tool registration, model prompt injection or channel API.
All operations demand a currently bound, revalidated AgentContext.
"""
from __future__ import annotations

import os
import re
import time
from uuid import uuid4

import agent_context
import agent_mailbox_db as db

MAX_BODY_BYTES = 8192
MAX_DEPTH = 1
MAX_ATTEMPTS = 8
_KINDS = frozenset({"message", "task_request", "task_result"})
_STATES = frozenset({"queued", "leased", "done", "failed", "dead"})
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class MailboxDenied(ValueError):
    pass


class MailboxConflict(ValueError):
    pass


def enabled(settings) -> bool:
    from agent_security import killed
    return (not killed() and agent_context.enabled(settings) and
            os.environ.get("CONVEYOR_AGENT_MAILBOX", "").lower()
            in ("1", "true", "yes", "on"))


def _require(settings, actor: agent_context.AgentContext):
    if not enabled(settings):
        raise MailboxDenied("agent mailbox disabled")
    bound = agent_context.current()
    if not isinstance(actor, agent_context.AgentContext) or bound != actor:
        raise MailboxDenied("bound agent identity required")
    try:
        # Reject stale/archived agents, forged owners and enlarged capabilities.
        return agent_context.validated(
            settings, actor, actor.origin_channel, agent_context._chat_id(actor),
            actor.operator_identity)
    except (agent_context.AgentContextDenied, TypeError) as exc:
        raise MailboxDenied("agent identity denied") from exc


def _target(settings, actor, to_agent):
    import agents
    if not isinstance(to_agent, str) or not re.fullmatch(r"[a-z0-9]{1,32}", to_agent):
        raise MailboxDenied("invalid recipient")
    if to_agent == actor.agent_id:
        raise MailboxDenied("self messaging is disabled")
    # Named-agent chat IDs are Web-only until the explicit channel router PR.
    # A Telegram/Feishu operator cannot assume the web-console owner's scope.
    try:
        target = agent_context.resolve(
            settings, "web", agents.chat_id_for(to_agent), agents.WEB_OPERATOR)
    except agent_context.AgentContextDenied as exc:
        raise MailboxDenied("recipient missing or archived") from exc
    if target.agent_id != to_agent or target.operator_identity != actor.operator_identity:
        raise MailboxDenied("cross-operator mailbox access denied")
    return target


def _now(value):
    return time.time() if value is None else float(value)


def _taint(settings, actor):
    # Untrusted inter-agent text is also conservatively a private-data read
    # for deployments with the optional egress-taint policy enabled.
    import egress_taint
    egress_taint.mark_read(
        settings, "memory.search", operator_id=actor.operator_identity,
        channel=actor.origin_channel, chat_id=agent_context._chat_id(actor))


def send(settings, *, sender: agent_context.AgentContext, to_agent: str,
         kind: str, body: str, idempotency_key: str,
         parent_message_id: str | None = None, depth: int = 0,
         max_attempts: int = 3, deadline: float | None = None,
         now: float | None = None) -> dict:
    actor = _require(settings, sender)
    _target(settings, actor, to_agent)
    timestamp = _now(now)
    import agent_delegation
    valid_kinds = (_KINDS | {"task", "result"}) if agent_delegation.enabled(settings) else _KINDS
    if not isinstance(kind, str) or kind not in valid_kinds or not isinstance(body, str) or not body.strip():
        raise MailboxDenied("invalid message type or body")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise MailboxDenied("message body too large")
    if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
        raise MailboxDenied("invalid idempotency key")
    if type(depth) is not int or not 0 <= depth <= MAX_DEPTH:
        raise MailboxDenied("delegation depth exceeded")
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise MailboxDenied("invalid retry budget")
    if (parent_message_id is None and depth != 0 or
        parent_message_id is not None and depth != 1):
        raise MailboxDenied("invalid parent/depth")
    explicit_deadline = deadline is not None
    deadline = timestamp + 3600 if deadline is None else float(deadline)

    with db.transaction(settings) as conn:
        db.ensure_active(conn, actor.agent_id, to_agent)
        existing = conn.execute(
            """SELECT * FROM agent_mailbox WHERE from_agent=? AND to_agent=?
               AND idempotency_key=?""",
            (actor.agent_id, to_agent, idempotency_key)).fetchone()
        if existing is not None:
            if (existing["operator"] != actor.operator_identity or
                existing["kind"] != kind or existing["body"] != body or
                existing["depth"] != depth or
                existing["parent_message_id"] != parent_message_id or
                existing["max_attempts"] != max_attempts or
                (explicit_deadline and existing["deadline"] != deadline)):
                raise MailboxConflict("idempotency key reused with different message")
            return db.public_row(existing)
        if not timestamp < deadline <= timestamp + 7 * 86400:
            raise MailboxDenied("invalid deadline")
        import agent_security
        agent_security.admit(conn, operator=actor.operator_identity,
                             to_agent=to_agent, kind=kind, now=timestamp)
        if parent_message_id is not None:
            parent = conn.execute(
                "SELECT * FROM agent_mailbox WHERE id=?", (parent_message_id,)
            ).fetchone()
            if (parent is None or parent["operator"] != actor.operator_identity or
                parent["to_agent"] != actor.agent_id or parent["depth"] != 0):
                raise MailboxDenied("invalid parent message")
        identifier = uuid4().hex
        conn.execute(
            """INSERT INTO agent_mailbox
               (id,from_agent,to_agent,operator,kind,body,idempotency_key,
                max_attempts,deadline,created_at,parent_message_id,depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, actor.agent_id, to_agent, actor.operator_identity,
             kind, body, idempotency_key, max_attempts, deadline, timestamp,
             parent_message_id, depth))
        import agent_security
        agent_security.audit_tx(conn, operator=actor.operator_identity,
            event="delegate",
            agent_id=to_agent, reason="queued", now=timestamp)
        return db.public_row(conn.execute(
            "SELECT * FROM agent_mailbox WHERE id=?", (identifier,)).fetchone())


def lease(settings, *, recipient: agent_context.AgentContext, worker_id: str,
          lease_seconds: int = 30, now: float | None = None) -> dict | None:
    actor = _require(settings, recipient)
    if (not isinstance(worker_id, str) or not _KEY.fullmatch(worker_id) or
        type(lease_seconds) is not int or not 1 <= lease_seconds <= 300):
        raise MailboxDenied("invalid lease")
    timestamp = _now(now)
    with db.transaction(settings) as conn:
        db.ensure_active(conn, actor.agent_id)
        db.expire(conn, actor.agent_id, actor.operator_identity, timestamp)
        row = conn.execute(
            """SELECT * FROM agent_mailbox WHERE to_agent=? AND operator=?
               AND attempts<max_attempts AND deadline>?
               AND (status IN ('queued','failed') OR
                    (status='leased' AND lease_expires_at<=?))
               ORDER BY created_at,id LIMIT 1""",
            (actor.agent_id, actor.operator_identity, timestamp, timestamp)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE agent_mailbox SET status='leased', lease_owner=?,
               lease_expires_at=?, attempts=attempts+1 WHERE id=?""",
            (worker_id, min(timestamp + lease_seconds, row["deadline"]), row["id"]))
        result = db.public_row(conn.execute(
            "SELECT * FROM agent_mailbox WHERE id=?", (row["id"],)).fetchone())
    _taint(settings, actor)
    return result


def _transition(settings, *, recipient, message_id, worker_id,
                failure: bool, now: float | None = None) -> dict | None:
    actor = _require(settings, recipient)
    if (not isinstance(worker_id, str) or not _KEY.fullmatch(worker_id) or
        not isinstance(message_id, str) or not re.fullmatch(r"[0-9a-f]{32}", message_id)):
        raise MailboxDenied("invalid lease identity")
    timestamp = _now(now)
    with db.transaction(settings) as conn:
        db.ensure_active(conn, actor.agent_id)
        db.expire(conn, actor.agent_id, actor.operator_identity, timestamp)
        row = conn.execute(
            """SELECT * FROM agent_mailbox WHERE id=? AND to_agent=?
               AND operator=? AND status='leased' AND lease_owner=?
               AND lease_expires_at>? AND deadline>?""",
            (message_id, actor.agent_id, actor.operator_identity,
             worker_id, timestamp, timestamp)).fetchone()
        if row is None:
            return None
        status = ("dead" if row["attempts"] >= row["max_attempts"]
                  or row["deadline"] <= timestamp
                  else "failed") if failure else "done"
        conn.execute(
            """UPDATE agent_mailbox SET status=?, lease_owner=NULL,
               lease_expires_at=NULL WHERE id=?""",
            (status, message_id))
        return db.public_row(conn.execute(
            "SELECT * FROM agent_mailbox WHERE id=?", (message_id,)).fetchone())


def ack(settings, *, recipient: agent_context.AgentContext, message_id: str,
        worker_id: str, now: float | None = None) -> dict | None:
    return _transition(settings, recipient=recipient, message_id=message_id,
                       worker_id=worker_id, failure=False, now=now)


def fail(settings, *, recipient: agent_context.AgentContext, message_id: str,
         worker_id: str, now: float | None = None) -> dict | None:
    return _transition(settings, recipient=recipient, message_id=message_id,
                       worker_id=worker_id, failure=True, now=now)


def list_for_agent(settings, *, recipient: agent_context.AgentContext,
                   status: str | None = None, limit: int = 50,
                   now: float | None = None) -> list[dict]:
    actor = _require(settings, recipient)
    if status is not None and status not in _STATES:
        raise MailboxDenied("invalid mailbox status")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise MailboxDenied("invalid list limit")
    with db.transaction(settings) as conn:
        db.ensure_active(conn, actor.agent_id)
        db.expire(conn, actor.agent_id, actor.operator_identity, _now(now))
        rows = conn.execute(
            """SELECT * FROM agent_mailbox WHERE to_agent=? AND operator=?
               AND (? IS NULL OR status=?) ORDER BY created_at DESC,id DESC
               LIMIT ?""",
            (actor.agent_id, actor.operator_identity, status, status, limit)
        ).fetchall()
    if rows:
        _taint(settings, actor)
    return [db.public_row(row) for row in rows]
