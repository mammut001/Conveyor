"""personal_tools/queue_tools.py — Queue status adapter for personal tools.

Provides queue.status as a READ tool that returns the same information
as the /queue command.
"""
from __future__ import annotations

from config import Settings
from personal_tools.base import ToolResult


async def queue_status_adapter(
    settings: Settings,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    """Return queue status (same as /queue command)."""
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    status = await queue.get_queue_status()
    return ToolResult(ok=True, text=status)
