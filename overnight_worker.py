"""Three role workers; handlers are injected so execution can be independently tested."""
from __future__ import annotations
import asyncio
from contextlib import suppress

ROLES = ('discover', 'repair', 'verify')


async def execute_claim(store, task, handler, *, lease_seconds=60, heartbeat_seconds=10):
    if not 0 < heartbeat_seconds < lease_seconds:
        raise ValueError('heartbeat must be shorter than lease')
    job = asyncio.create_task(handler(task))
    try:
        while not job.done():
            done, _ = await asyncio.wait({job}, timeout=heartbeat_seconds)
            # Fence results even if cancellation or deadline raced with completion.
            valid = await asyncio.to_thread(store.renew, task['id'], task['lease'], lease_seconds=lease_seconds)
            if not valid:
                job.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await job
                return False
            if done:
                break
        try:
            result = job.result()
        except Exception as exc:
            # Persist type only: provider errors can contain credentials.
            return await asyncio.to_thread(store.finish, task['id'], task['lease'],
                                           {'error':type(exc).__name__}, success=False)
        return await asyncio.to_thread(store.finish, task['id'], task['lease'], result)
    finally:
        if not job.done():
            job.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await job


async def serve(store, run_id, handlers, *, poll_seconds=1):
    if set(handlers) != set(ROLES):
        raise ValueError('all three role handlers are required')

    async def worker(role):
        while True:
            status = await asyncio.to_thread(store.status, run_id)
            import time
            if status['state'] != 'running' or status['deadline'] <= time.time():
                return
            task = await asyncio.to_thread(store.claim, run_id, role, lease_seconds=60)
            if task is None:
                await asyncio.sleep(poll_seconds)
                continue
            await execute_claim(store, task, handlers[role])

    workers = [asyncio.create_task(worker(role), name=f'overnight-{role}') for role in ROLES]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
