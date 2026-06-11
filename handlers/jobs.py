"""handlers/jobs.py — start a Codex job, route progress + final reply.

Behavior:
- Sends a "⏳ 收到, 处理中..." placeholder via port.reply.
- Calls runner.start(mode, prompt, on_progress).
- For each progress event, port.edit_progress; if the adapter latches
  (returns False), port.send_new takes over with a mode-aware cap so
  Feishu (which cannot edit_progress yet) does not spam a fresh
  bubble per intermediate event.
- On completion, port.send_new with job.summary (or error).

This is the same flow as bot.py::_start_job and feishu_bot.py's
_start_job; both delegate here.

Progress verbosity is controlled by
``Settings.conveyor_progress_mode``:
- ``verbose``: every Codex event reaches the chat (debug-friendly).
- ``compact`` (default): agent prose is suppressed; tool indicators,
  the thinking indicator, and tool pulses still reach the chat.
  When the channel cannot edit_progress, at most one fallback
  "仍在处理..." message is sent per job.
- ``quiet``: no intermediate progress at all; only the initial
  placeholder and the final summary reach the chat.

The mode is also enforced inside ``runner/streaming.py`` so a direct
``runner.start`` call (e.g. from a future tool harness) honors the
same policy. The defense in ``progress()`` here is the second
layer, applied to whatever survives the streaming filter.
"""
from __future__ import annotations

import logging

from channel.types import InboundMessage, OutboundPort
from config import Settings
from redaction import truncate
from runner import CodexRunner, JobMode, JobState

logger = logging.getLogger(__name__)

PLACEHOLDER_TEXT = "⏳ 收到，处理中..."

# Compact-mode fallback status when the channel cannot edit_progress
# (e.g. Feishu, or after a Telegram latch fires). One status line
# per job keeps the chat clean instead of dropping a fresh bubble
# on every progress callback.
COMPACT_FALLBACK_TEXT = "仍在处理..."

_TOOL_INDICATOR_PREFIX = ("🔧", "\U0001f527")
_THOUGHT_INDICATOR_PREFIX = ("💭", "\U0001f4ad")
_TOOL_PULSE_PREFIX = ("🔧", "\U0001f527")


def _is_prose_progress_text(text: str) -> bool:
    """True when ``text`` is a chunk of agent prose (top-level
    message / summary / text / delta or ``agent_message`` text) rather
    than a tool indicator, thinking indicator, or tool pulse.

    Mirrors the prefix check in ``runner/streaming.py``; kept here so
    handlers/jobs.py can defend the final-answer path even if a
    direct ``port.edit_progress`` call is fed by a future tool that
    bypasses the streaming filter.
    """
    if not text:
        return False
    if text.startswith(_TOOL_INDICATOR_PREFIX) or text.startswith(_TOOL_PULSE_PREFIX):
        return False
    if text.startswith(_THOUGHT_INDICATOR_PREFIX):
        return False
    return True


def _normalize_mode(mode: str | None) -> str:
    if mode in ("verbose", "compact", "quiet"):
        return mode
    return "compact"


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

    progress_mode = _normalize_mode(getattr(runner.settings, "conveyor_progress_mode", "compact"))

    placeholder_id = await port.reply(msg, PLACEHOLDER_TEXT)
    last_progress: str = PLACEHOLDER_TEXT
    edit_broken = False
    # Compact-mode latch: when edit_progress fails once, we send at
    # most one COMPACT_FALLBACK_TEXT so the chat is not peppered
    # with "🔧 curl..." / "我这就帮你查一下。" bubbles. Quiet mode
    # never sends a fallback.
    compact_fallback_sent = False
    prose_already_dropped = False  # to keep `last_progress` consistent

    async def progress(message_text: str) -> None:
        nonlocal last_progress, edit_broken, compact_fallback_sent
        outgoing = truncate(message_text)
        if outgoing == last_progress:
            return
        if progress_mode == "quiet":
            # Suppress every intermediate. We still update
            # last_progress to keep the dedupe math working for
            # the final-answer de-dup (otherwise a quiet-mode
            # tool call that fired before the final summary
            # would re-trigger the "summary != last_progress"
            # send). Not updating last_progress here would
            # actually be safe (we never call on_progress in
            # quiet, by definition), but we keep the update for
            # defense-in-depth in case a future caller adds
            # custom on_progress paths.
            last_progress = outgoing
            return
        if progress_mode == "compact" and _is_prose_progress_text(outgoing):
            # Drop agent prose in compact mode. Do NOT update
            # last_progress; if a tool call follows immediately
            # and the channel latched, the next progress should
            # still surface (and our dedupe gate compares against
            # the most recent forwarded text, not every seen
            # text).
            prose_already_dropped = True
            return
        if placeholder_id is not None and not edit_broken:
            ok = await port.edit_progress(msg, placeholder_id, outgoing)
            if ok:
                last_progress = outgoing
                return
            edit_broken = True
            if progress_mode == "compact":
                # Latch + compact: at most one fallback. We do
                # NOT call port.send_new for the original
                # outgoing; we substitute a single compact
                # status line and update last_progress so the
                # final-answer dedupe compares against that.
                await port.send_new(msg, COMPACT_FALLBACK_TEXT)
                last_progress = COMPACT_FALLBACK_TEXT
                compact_fallback_sent = True
                return
            if progress_mode == "quiet":
                # Latch + quiet: no fallback at all. Just remember
                # the placeholder is dead.
                last_progress = outgoing
                return
        # Already latched AND in compact/quiet: drop further
        # intermediate progress entirely. Without this guard the
        # third and fourth tool indicators would still call
        # port.send_new below and re-create the chat spam we are
        # trying to fix. Only verbose is allowed to keep flooding
        # the chat (debug-friendly legacy mode).
        if edit_broken and progress_mode in ("compact", "quiet"):
            last_progress = outgoing
            return
        # verbose (or the edit_broken branch in verbose): forward
        # the original outgoing text. In verbose the historical
        # behavior is preserved (one new message per progress).
        await port.send_new(msg, outgoing)
        last_progress = outgoing

    try:
        job = await runner.start(mode, body, progress)
    except Exception as exc:
        await port.reply(msg, f"现在不能开始：{truncate(str(exc), 1200)}")
        return

    # Wait for completion (runner.start spawns the task; we await state
    # transitions to keep port lifecycle simple). The progress callback may
    # have already sent the final answer (e.g. feishu's edit_progress always
    # returns False, so each progress step — including the last — is sent as
    # a new message and recorded in last_progress). Re-send only if the
    # final summary differs from what we already delivered, so the user does
    # not see the same paragraph twice.
    while job.state == JobState.RUNNING:
        await _sleep(0.3)
    if job.summary:
        summary = job.summary
        # Round-9 de-dup. The runner's terminal on_progress already
        # delivered a (truncated) final message, and that truncated
        # text is what `last_progress` recorded. Compare against the
        # same truncation so long answers that differ only in
        # post-truncation characters do not get re-sent as a second
        # message. Same logic for the error and last_progress
        # branches below.
        summary_truncated = truncate(summary)
        if summary_truncated.strip() != last_progress.strip():
            await port.send_new(msg, summary)
    elif job.error:
        err_truncated = truncate(job.error, 3500)
        if err_truncated.strip() != last_progress.strip():
            await port.send_new(msg, err_truncated)
    elif last_progress and last_progress != PLACEHOLDER_TEXT:
        await port.send_new(msg, last_progress)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
