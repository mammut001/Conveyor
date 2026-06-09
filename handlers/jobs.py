"""handlers/jobs.py — start a Codex job, route progress + final reply.

Behavior:
- Sends a "⏳ 收到, 处理中..." placeholder via port.reply.
- Calls runner.start(mode, prompt, on_progress).
- For each progress event, port.edit_progress; if the adapter latches
  (returns False), port.send_new takes over.
- On completion, port.send_new with job.summary (or error).

This is the same flow as bot.py::_start_job and feishu_bot.py's
_start_job; both delegate here.
"""
from __future__ import annotations

import logging

from channel.types import InboundMessage, OutboundPort
from redaction import truncate
from runner import CodexRunner, JobMode, JobState

logger = logging.getLogger(__name__)

PLACEHOLDER_TEXT = "⏳ 收到，处理中..."


async def handle_codex_job(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    mode: JobMode = JobMode.RUN,
    prompt: str | None = None,
) -> None:
    body = (prompt if prompt is not None else msg.text).strip()
    if not body:
        await port.reply(msg, "Usage: /run <prompt>")
        return

    placeholder_id = await port.reply(msg, PLACEHOLDER_TEXT)
    last_progress: str = PLACEHOLDER_TEXT
    edit_broken = False

    async def progress(message_text: str) -> None:
        nonlocal last_progress, edit_broken
        outgoing = truncate(message_text)
        if outgoing == last_progress:
            return
        if placeholder_id is not None and not edit_broken:
            ok = await port.edit_progress(msg, placeholder_id, outgoing)
            if ok:
                last_progress = outgoing
                return
            edit_broken = True
        await port.send_new(msg, outgoing)
        last_progress = outgoing

    try:
        job = await runner.start(mode, body, progress)
    except Exception as exc:
        await port.reply(msg, f"现在不能开始：{truncate(str(exc), 1200)}")
        return

    # Wait for completion (runner.start spawns the task; we await state
    # transitions to keep port lifecycle simple).
    while job.state == JobState.RUNNING:
        await _sleep(0.3)
    if job.summary:
        await port.send_new(msg, job.summary)
    elif job.error:
        await port.send_new(msg, truncate(job.error, 3500))
    elif last_progress and last_progress != PLACEHOLDER_TEXT:
        await port.send_new(msg, last_progress)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
