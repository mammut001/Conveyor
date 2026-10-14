"""Cancellation-resistant, bounded G4 Chromium shutdown.

Runs in a single owned asyncio Task; callers await shield(task). No awaits
occur in the final filesystem/ownership cleanup after a cancellation.
"""
from __future__ import annotations

import asyncio
import shutil

from browser_auth_owner import release


_WIPE = """async () => {
    localStorage.clear(); sessionStorage.clear();
    for(const k of await caches.keys()) await caches.delete(k);
    if (indexedDB.databases) for(const db of await indexedDB.databases())
      if(db.name) indexedDB.deleteDatabase(db.name);
}"""


async def bounded_close(session):
    try:
        watch = session._watch
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        context = session.context
        if context is not None:
            # One two-second bound for all pages, not N * 2 seconds.
            try:
                pages = list(context.pages)
                if pages:
                    await asyncio.wait_for(asyncio.gather(
                        *(page.evaluate(_WIPE) for page in pages),
                        return_exceptions=True), timeout=2)
            except (Exception, asyncio.TimeoutError):
                pass
            try:
                await asyncio.wait_for(context.clear_cookies(), timeout=2)
            except (Exception, asyncio.TimeoutError):
                pass
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except (Exception, asyncio.TimeoutError):
                pass
        pw = session.owner._drivers.pop(session.id, None)
        if pw is not None:
            try:
                await asyncio.wait_for(pw.stop(), timeout=5)
            except (Exception, asyncio.TimeoutError):
                pass
    finally:
        # If evaluation, transport shutdown, or this close Task was cancelled,
        # the directory must never survive due to an unbounded finally await.
        # Recover driver reference if an earlier stage was interrupted.
        pw = session.owner._drivers.pop(session.id, None)
        if pw is not None:
            # stop() was not reached; asyncio cleanup must not be awaited here.
            pass
        try:
            shutil.rmtree(session.directory, ignore_errors=True)
        finally:
            release(session._owner_lock)
            session._owner_lock = None
