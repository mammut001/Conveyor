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

P3.8: Adds job queue integration. If a Codex job is running, new
jobs are queued instead of rejected. Actual Codex execution remains
single-concurrency.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from channel.types import InboundMessage, OutboundPort
from config import Settings
from redaction import truncate
from runner import CodexRunner, JobMode, JobState

if TYPE_CHECKING:
    from handlers.job_queue import QueuedJob

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

    # P3.8: Check if a job is already running and queue if needed
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()

    if runner.current_job and runner.current_job.state == JobState.RUNNING:
        # Job is running, queue this one
        success, queue_msg, queued_job = await queue.enqueue(
            mode=mode.value,
            prompt=body,
            msg=msg,
            port=port,
            runner=runner,
            original_text=msg.text,
        )
        if success:
            await port.reply(msg, queue_msg)
        else:
            await port.reply(msg, f"无法排队：{queue_msg}")
        return

    # No job running, execute immediately
    await _execute_codex_job(msg, port, runner, mode, body)


async def _execute_codex_job(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    mode: JobMode,
    body: str,
) -> None:
    """Execute a Codex job directly (not through queue)."""
    # Session context injection: prepend recent turns so the LLM has
    # continuity when the user says "继续" / "continue". Only for LLM
    # jobs (handle_codex_job is only called for /run, /fix, and free
    # text fallback — never for deterministic commands).
    from handlers.session import build_context_prompt, append_turn
    ctx_prompt = build_context_prompt(runner.settings, msg)
    user_text_for_session = body  # remember for session recording

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

    # Prepend session context if available.
    effective_body = (ctx_prompt + body) if ctx_prompt else body

    try:
        job = await runner.start(mode, effective_body, progress)
    except Exception as exc:
        # Failure to even start the job (e.g. invalid args, Codex
        # missing). On Feishu, surface this as a card; Telegram keeps
        # the existing text path.
        if msg.channel == "feishu" and hasattr(port, "send_card"):
            try:
                from channel.feishu_cards import job_failed_card
                await port.send_card(msg, job_failed_card(
                    job_id="(start-failed)",
                    error=f"现在不能开始：{truncate(str(exc), 1200)}",
                ))
                return
            except Exception:
                pass
        await port.reply(msg, f"现在不能开始：{truncate(str(exc), 1200)}")
        return

    # On Feishu, send a structured "job started" card right after
    # the runner accepts the job. The placeholder ("⏳ 收到，处理中…")
    # already went out as an editable card via port.reply, so the
    # started card is a fresh message — operator chat stays clean
    # and the buttons let them jump to status / diff / cancel without
    # retyping. Telegram ignores the new path and uses the existing
    # final-summary flow.
    if msg.channel == "feishu" and hasattr(port, "send_card"):
        try:
            from channel.feishu_cards import job_started_card
            worktree = getattr(getattr(job, "worktree", None), "path", None) \
                or getattr(job, "worktree_path", None)
            await port.send_card(msg, job_started_card(
                job_id=str(getattr(job, "id", "")),
                prompt=body,
                worktree=str(worktree) if worktree else None,
            ))
        except Exception:
            logger.debug("Feishu job_started_card failed", exc_info=True)

    # Wait for completion (runner.start spawns the task; we await state
    # transitions to keep port lifecycle simple). The progress callback may
    # have already sent the final answer (e.g. feishu's edit_progress always
    # returns False, so each progress step — including the last — is sent as
    # a new message and recorded in last_progress). Re-send only if the
    # final summary differs from what we already delivered, so the user does
    # not see the same paragraph twice.
    while job.state == JobState.RUNNING:
        await _sleep(0.3)

    # Determine the final assistant text for session recording.
    final_answer = ""
    job_id = str(getattr(job, "id", ""))
    is_feishu = msg.channel == "feishu" and hasattr(port, "send_card")
    if job.summary:
        summary = job.summary
        final_answer = summary
        # Round-9 de-dup. The runner's terminal on_progress already
        # delivered a (truncated) final message, and that truncated
        # text is what `last_progress` recorded. Compare against the
        # same truncation so long answers that differ only in
        # post-truncation characters do not get re-sent as a second
        # message. Same logic for the error and last_progress
        # branches below.
        summary_truncated = truncate(summary)
        if summary_truncated.strip() != last_progress.strip():
            if is_feishu:
                try:
                    from channel.feishu_cards import job_finished_card
                    await port.send_card(msg, job_finished_card(
                        job_id=job_id,
                        summary=summary,
                    ))
                except Exception:
                    logger.debug("Feishu job_finished_card failed", exc_info=True)
                    await port.send_new(msg, summary)
            else:
                await port.send_new(msg, summary)
    elif job.error:
        err_truncated = truncate(job.error, 3500)
        final_answer = f"[error] {err_truncated}"
        if err_truncated.strip() != last_progress.strip():
            if is_feishu:
                try:
                    from channel.feishu_cards import job_failed_card
                    await port.send_card(msg, job_failed_card(
                        job_id=job_id,
                        error=err_truncated,
                    ))
                except Exception:
                    logger.debug("Feishu job_failed_card failed", exc_info=True)
                    await port.send_new(msg, err_truncated)
            else:
                await port.send_new(msg, err_truncated)
    elif last_progress and last_progress != PLACEHOLDER_TEXT:
        final_answer = last_progress
        await port.send_new(msg, last_progress)

    # Record turn for session continuity.
    append_turn(runner.settings, msg, user_text_for_session, final_answer)

    # P3.8: Notify queue that job completed, so next queued job can start
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    await queue.on_job_completed(job.id)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
