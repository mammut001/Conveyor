"""Stop an agent process together with everything it started.

Agent CLIs (codex, claude, pi, other CLIs) start npm, test servers, git and
shells. Those grandchildren inherit the agent's stdout/stderr pipes. Killing
only the agent leaves the pipes open, so ``process.wait()`` and the stream
readers never return and the job never reaches a final state.

Agents are therefore started with ``start_new_session=True`` (their pid is
then also their process group id), stopped by signalling the whole group,
and their readers are given a bounded time to finish after the agent exits.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any, Awaitable, Callable, Iterable

# SIGTERM to SIGKILL on timeout. Cancel keeps its own, longer grace.
AGENT_TERM_GRACE_SECONDS = 5.0
# How long stdout/stderr readers may go without consuming output (after the
# agent itself exited) before they count as held open by a grandchild.
STREAM_DRAIN_SECONDS = 5.0
# Hard cap on the whole drain, however much output keeps arriving.
STREAM_DRAIN_MAX_SECONDS = 180.0
_DRAIN_POLL_SECONDS = 0.05
# How long to wait for the agent to be reaped after SIGKILL.
KILL_WAIT_SECONDS = 5.0
# asyncio's process.wait() also waits for the pipes to close; a grandchild
# holding them would hang it. returncode is set when the agent itself exits.
_EXIT_POLL_SECONDS = 0.1


def signal_group(process: Any, sig: int) -> None:
    """Send ``sig`` to the process group led by ``process``.

    Falls back to the process alone when it does not lead a group (or the
    platform has no killpg), so a caller never loses the signal.
    """
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            pass
        except OSError:
            pass
    if getattr(process, "returncode", None) is not None:
        return
    try:
        if sig == signal.SIGKILL and hasattr(process, "kill"):
            process.kill()
        elif sig == signal.SIGTERM and hasattr(process, "terminate"):
            process.terminate()
        else:
            process.send_signal(sig)
    except ProcessLookupError:
        pass


async def wait_exit(process: Any, timeout: float | None = None) -> int | None:
    """Wait for the agent to exit, even while a grandchild keeps its pipes open.

    Raises ``asyncio.TimeoutError`` after ``timeout`` seconds.
    """

    async def poll() -> int | None:
        while getattr(process, "returncode", None) is None:
            await asyncio.sleep(_EXIT_POLL_SECONDS)
        return process.returncode

    waiters = {asyncio.ensure_future(process.wait()), asyncio.ensure_future(poll())}
    try:
        done, _pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
    if not done:
        raise asyncio.TimeoutError
    if getattr(process, "returncode", None) is not None:
        return process.returncode
    return next(iter(done)).result()


async def stop_group(process: Any, *, grace: float = AGENT_TERM_GRACE_SECONDS) -> int | None:
    """SIGTERM the agent's group, SIGKILL it after ``grace``; return the exit code.

    The group is killed even when the agent exits within the grace period,
    so a child that ignored SIGTERM does not outlive the job.
    """
    if getattr(process, "returncode", None) is None:
        signal_group(process, signal.SIGTERM)
        try:
            await wait_exit(process, grace)
        except asyncio.TimeoutError:
            pass
    signal_group(process, signal.SIGKILL)
    try:
        return await wait_exit(process, KILL_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return getattr(process, "returncode", None)


class StreamActivity:
    """Clock a stream reader updates while it consumes the agent's output.

    ``touch`` is called for every line a reader takes from the pipe.
    ``progress`` wraps the reader's progress callback: while a callback runs
    the reader counts as busy, so a slow Telegram/Feishu edit is never mistaken
    for a pipe that is held open with nothing left to read.
    """

    def __init__(self) -> None:
        self.last = time.monotonic()
        self.busy = 0

    def touch(self) -> None:
        self.last = time.monotonic()

    def progress(self, callback: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def tracked(*args: Any, **kwargs: Any) -> Any:
            self.busy += 1
            try:
                return await callback(*args, **kwargs)
            finally:
                self.busy -= 1
                self.touch()

        return tracked


async def finish_readers(
    process: Any,
    tasks: Iterable[asyncio.Future],
    *,
    timeout: float | None = None,
    activity: StreamActivity | None = None,
    max_seconds: float | None = None,
) -> list[Any]:
    """Results of the stream readers, after the agent has exited.

    The readers get until ``timeout`` seconds pass with no activity (see
    ``StreamActivity``) to finish. Going quiet that long means something left
    in the agent's group holds the pipe: the group is killed, and a reader
    that still does not finish is cancelled. ``max_seconds`` caps the total
    wait, so a grandchild that writes forever still cannot hang the job.
    Returns ``gather(..., return_exceptions=True)`` results in the order given.
    """
    tasks = list(tasks)
    if not tasks:
        return []
    if timeout is None:
        timeout = STREAM_DRAIN_SECONDS
    if max_seconds is None:
        max_seconds = STREAM_DRAIN_MAX_SECONDS
    if activity is None:
        activity = StreamActivity()
    hard_deadline = time.monotonic() + max_seconds
    pending: set[asyncio.Future] = set(tasks)
    while pending:
        now = time.monotonic()
        if now >= hard_deadline:
            break
        if not activity.busy and now - activity.last >= timeout:
            break
        _done, pending = await asyncio.wait(pending, timeout=_DRAIN_POLL_SECONDS)
    if pending:
        signal_group(process, signal.SIGKILL)
        _done, pending = await asyncio.wait(pending, timeout=1.0)
        for task in pending:
            task.cancel()
    return await asyncio.gather(*tasks, return_exceptions=True)
