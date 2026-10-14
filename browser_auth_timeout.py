"""Named bounded waits for G4 startup and credential-safe browser steps."""
from __future__ import annotations

import asyncio


async def wait_for_step(awaitable, seconds, name):
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except asyncio.TimeoutError as exc:
        # Static step names only; never include URLs, credentials or page text.
        raise asyncio.TimeoutError(f"g4_timeout:{name}") from exc
