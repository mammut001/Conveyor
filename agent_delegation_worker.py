"""Opt-in durable delegation polling worker with database-wide concurrency cap."""
from __future__ import annotations

import asyncio
import logging
import time

import agent_context as ac
import agent_delegation
import agent_delegation_queue as queue
import agent_mailbox as mailbox
import agent_mailbox_db as db

log = logging.getLogger(__name__)
_running: dict[tuple[str, str], asyncio.Task] = {}


def enabled(settings):
    return agent_delegation.enabled(settings)


def cancel_operator(settings, operator_id):
    """Authenticated emergency stop makes all queued/running tasks terminal."""
    if not enabled(settings):
        return 0
    count = queue.cancel_operator(settings, operator_id)
    for (operator, _identifier), task in tuple(_running.items()):
        if operator == operator_id and not task.done():
            task.cancel()
    return count


def cancel_all(settings):
    """Stop every delegated child after an inbound channel auth check.

    Delegations currently share the canonical Web Console operator, even
    when the authenticated stop arrives from Telegram or Feishu.
    """
    import agents
    return cancel_operator(settings, agents.WEB_OPERATOR)


async def _finish(settings, record, worker_id):
    import agents
    from agent_delegation_turn import execute
    try:
        target = ac.resolve(settings, "web",
                            agents.chat_id_for(record["to_agent"]),
                            record["operator"])
    except ac.AgentContextDenied:
        queue.reject_revoked(settings, record, worker_id)
        _running.pop((record["operator"], record["id"]), None)
        return
    try:
        summary = await execute(settings, record)
        if not enabled(settings):
            raise asyncio.CancelledError()
        # Enqueue the result and ACK inside ONE lease-checked transaction.
        # A stop/lease expiry can no longer race a separate result write.
        with ac.bind(target):
            committed = queue.complete(settings, target, record, worker_id,
                                       summary or "(no result)")
            if not committed:
                log.warning("Delegation lease expired or stopped: %s", record["id"])
    except asyncio.CancelledError:
        # Emergency stop already marks tasks dead; do not requeue it.
        with ac.bind(target):
            try:
                mailbox.fail(settings, recipient=target,
                             message_id=record["id"], worker_id=worker_id)
            except Exception:
                log.debug("Cannot fail cancelled delegation", exc_info=True)
        raise
    except Exception:
        log.exception("Delegation failed: %s", record["id"])
        with ac.bind(target):
            try:
                mailbox.fail(settings, recipient=target,
                             message_id=record["id"], worker_id=worker_id)
            except Exception:
                log.exception("Unable to mark delegated task failed")
    finally:
        _running.pop((record["operator"], record["id"]), None)


async def poll_once(settings):
    """One bounded scan; ideal for tests and safe startup polling."""
    if not enabled(settings):
        return 0
    import agents
    started = 0
    for info in agents.AgentStore(settings).list():
        if not enabled(settings):
            return started
        if info.get("archived"):
            continue
        try:
            target = ac.resolve(settings, "web",
                                agents.chat_id_for(info["id"]),
                                agents.WEB_OPERATOR)
            with ac.bind(target):
                result = queue.claim(settings, target)
        except (ValueError, OSError):
            continue
        if result is None:
            continue
        row, worker_id = result
        job = asyncio.create_task(_finish(settings, row, worker_id),
                                  name="conveyor-delegation-" + row["id"][:10])
        _running[(row["operator"], row["id"])] = job
        started += 1
    return started


class Worker:
    def __init__(self, settings):
        self.settings = settings
        self.task = None

    async def run(self):
        while enabled(self.settings):
            try:
                await poll_once(self.settings)
            except Exception:
                log.exception("Delegation poll failed")
            await asyncio.sleep(0.5)

    def stop(self):
        if self.task:
            self.task.cancel()
        for task in list(_running.values()):
            task.cancel()


def start(loop, settings):
    """Only the primary Web Console starts polling; off means no side effects."""
    if not enabled(settings):
        return None
    worker = Worker(settings)
    worker.task = loop.create_task(worker.run(), name="conveyor-delegation-poller")
    return worker
