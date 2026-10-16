"""Lifecycle for routed-result pollers; never start PTB tasks in post_init."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram.ext import Application

log = logging.getLogger(__name__)

PollerFactory = Callable[[], Awaitable[None]]
_POLLER_KEY = "agent_result_poller"
_FACTORY_KEY = "_agent_result_poller_factory"


def _report_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "Agent result poller %s crashed", task.get_name(),
            exc_info=(type(error), error, error.__traceback__),
        )


def create_poller(factory: PollerFactory, *, name: str) -> asyncio.Task:
    """Create a running-loop task and always observe unexpected exceptions."""
    task = asyncio.create_task(factory(), name=name)
    task.add_done_callback(_report_failure)
    return task


async def cancel_poller(task: asyncio.Task | None) -> None:
    """Stop delivery before the bot/channel connection is shut down."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def configure_telegram_poller(application: Application, factory: PollerFactory) -> None:
    """Called from post_init to store a factory, not schedule a coroutine."""
    application.bot_data[_FACTORY_KEY] = factory


class AgentResultPollingApplication(Application):
    """PTB invokes start after post_init; stop precedes post_shutdown."""

    async def start(self) -> None:
        await super().start()
        factory = self.bot_data.get(_FACTORY_KEY)
        if factory is not None:
            task = self.bot_data.get(_POLLER_KEY)
            if task is None or task.done():
                self.bot_data[_POLLER_KEY] = create_poller(
                    factory, name="conveyor-agent-telegram-results",
                )

    async def stop(self) -> None:
        try:
            await cancel_poller(self.bot_data.pop(_POLLER_KEY, None))
        finally:
            await super().stop()
