"""runner/streaming.py — split out of runner.py.

The original runner.py was 2005 lines and 5 big
responsibilities. This file is one slice.

runner/core.py attaches each function on this module
to the CodexRunner class as a method at import time,
so callers see the same public surface.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text


def _is_reasoning_event(self, event: dict) -> bool:
    event_type = str(event.get("type") or event.get("event") or "").lower()
    if "reasoning" in event_type:
        return True
    item = event.get("item")
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").lower()
        if "reasoning" in item_type:
            return True
    if isinstance(item, str) and "reasoning" in item.lower():
        return True
    return False


def _is_tool_call_start_event(self, event_obj: dict | None) -> bool:
    """True for an ``item.started`` (or ``item.updated``) event whose
    item envelope is a function_call / tool_call / command_execution.
    Used by ``_read_jsonl_stdout`` to arm the round-6 tool-pulse
    window: a long tool call would otherwise leave the placeholder
    sitting on the one-line ``🔧 name...`` indicator for 5-30s
    with no further edits, which feels frozen. The arm fires the
    first time we see the tool invocation; the disarm is the
    matching ``_is_tool_call_complete_event`` for the same name.
    """
    if not isinstance(event_obj, dict):
        return False
    event_type = str(event_obj.get("type") or event_obj.get("event") or "").lower()
    # ``item.started`` is the canonical codex-CLI start event for a
    # tool invocation. ``item.updated`` is accepted too because
    # some codex builds emit the first tick of a tool call as an
    # updated envelope instead of started; both carry a tool-call
    # item, so reusing ``_tool_call_name`` is the right gate.
    if event_type not in ("item.started", "item.updated"):
        return False
    return self._tool_call_name(event_obj) is not None


def _is_tool_call_complete_event(self, event_obj: dict | None) -> bool:
    """True for an ``item.completed`` event whose item envelope is
    a function_call / tool_call / command_execution. Used by
    ``_read_jsonl_stdout`` to disarm the round-6 tool-pulse
    window. The disarm only fires when the completed tool name
    matches the currently armed name so a stale complete from a
    prior call cannot wipe a fresh arm.
    """
    if not isinstance(event_obj, dict):
        return False
    event_type = str(event_obj.get("type") or event_obj.get("event") or "").lower()
    if event_type != "item.completed":
        return False
    return self._tool_call_name(event_obj) is not None


async def _read_jsonl_stdout(
    self,
    job: Job,
    process: asyncio.subprocess.Process,
    on_progress: ProgressCallback,
) -> None:
    assert process.stdout is not None
    assert job.log_path is not None
    # Round-8 first-event cooldown bypass. The 3-gate chain below
    # (thinking indicator, prose, tool-pulse) all gate sends on
    # ``now - last_sent >= telegram_progress_seconds`` (3s default).
    # Initializing ``last_sent`` to 0.0 would force the very first
    # event in the stream to wait 3s after the loop started (= 3s
    # after ``_start_job`` called ``runner.start``) before passing
    # the cooldown, even though the placeholder already appeared at
    # T+0 sub-second. That 3-second gap made the chat look frozen
    # vs Hermes-style "first prose appears immediately". Seeding
    # ``last_sent`` at -telegram_progress_seconds makes the first
    # event pass the cooldown (``now - (-3.0) >= 3.0`` is always
    # true for ``now >= 0``). After the first send, ``last_sent``
    # is updated to ``now``, so the normal cooldown applies for
    # the rest of the stream. This is a one-shot bypass, not a
    # permanent lowering of the cooldown.
    last_sent = -self.settings.telegram_progress_seconds
    # Round-10 progress verbosity gate. Read once per stream so the
    # mode is stable for the whole job. ``quiet`` drops ALL
    # intermediate progress (placeholder survives untouched until
    # the final summary in handlers/jobs.py). ``compact`` drops
    # only agent prose; tool indicators, the thinking indicator,
    # and the tool-pulse still reach the chat. ``verbose`` is the
    # historical behavior.
    progress_mode = (self.settings.conveyor_progress_mode or "compact").lower()
    prose_allowed = _progress_mode_allows_prose(progress_mode)
    # Consecutive-same-text dedup: when codex emits
    # ``item.started`` + ``item.completed`` for the same tool call in
    # quick succession (typical for a short shell command), the
    # _event_summary produces the same indicator text twice. The
    # cooldown alone lets both through if they land within
    # ``telegram_progress_seconds`` of each other, so we also
    # suppress exact repeats of the last forwarded text. The raw
    # lines are still written to ``job.log_path``; only the user-
    # facing edit is skipped.
    last_sent_text: str | None = None
    # Per-stream "growing" gate for prose events. When codex
    # streams an ``agent_message`` (or a top-level text field) the
    # item text can briefly SHRINK mid-stream if the model
    # re-writes a paragraph. Editing the placeholder to a shorter
    # string makes the chat visibly re-write from char 1, which
    # feels like the model is "going backwards". Only forward
    # edits that strictly extend the last sent prose. ``item.
    # completed`` is exempt so the final text always wins; the
    # tracker resets on complete so the next item can be shorter
    # (and start a new growing chain).
    last_prose_text: str | None = None
    # Round-5 thinking indicator. Reasoning events stream silently
    # via ``_event_summary`` returning "" (runner.py:1406), so the
    # placeholder sits at the bot's initial "⏳ Got it, working on
    # it..." for 5-30s during a hard think and the chat feels
    # frozen. After ``THINKING_THRESHOLD_SECONDS`` of sustained
    # reasoning we surface a short "💭 thinking..." so the user
    # knows the model is alive. Any non-reasoning event (prose,
    # tool indicator, ``item.completed``, lifecycle, malformed
    # JSON) breaks the chain so the next reasoning burst starts
    # a fresh threshold window. ``thinking_indicator_sent`` is
    # per-chain: once we've emitted the indicator we don't keep
    # editing the placeholder on every reasoning tick.
    thinking_since: float | None = None
    thinking_indicator_sent = False
    # Round-6 tool-pulse state. A long tool call (network fetch,
    # big shell pipeline) would otherwise leave the placeholder
    # sitting on the one-line 🔧 name... indicator from
    # round 2 with no further edits for 5-30s, which reads as
    # frozen. ``pending_tool_name`` is the active tool call;
    # ``pending_tool_since`` is the wall time the arm fired;
    # ``last_pulse_at`` is the most recent pulse tick (None
    # between fires). Disarm is the matching ``item.completed``
    # event; the pulse is gated by ``TOOL_PULSE_THRESHOLD_SECONDS``
    # (first-fire delay) and ``TOOL_PULSE_INTERVAL_SECONDS``
    # (re-arm).
    pending_tool_name: str | None = None
    pending_tool_since: float | None = None
    last_pulse_at: float | None = None
    reconnect_stalls = 0
    with job.log_path.open("ab") as log_file:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            log_file.write(line)
            log_file.flush()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            event_text = self._event_summary(text)
            try:
                event_obj = json.loads(text)
            except json.JSONDecodeError:
                event_obj = None
            if isinstance(event_obj, dict) and str(event_obj.get("type") or "").lower() == "error":
                err_msg = str(event_obj.get("message") or "")
                err_lower = err_msg.lower()
                if "reconnecting" in err_lower or "high demand" in err_lower:
                    reconnect_stalls += 1
                    if reconnect_stalls >= RECONNECT_STALL_LIMIT:
                        job.error = truncate(err_msg, 500)
                        if job.process and job.process.returncode is None:
                            job.process.terminate()
                        break
                else:
                    reconnect_stalls = 0
            if event_text and not (isinstance(event_obj, dict) and self._is_reasoning_event(event_obj)):
                job.last_event = event_text
            self._capture_usage(job, text)
            now = asyncio.get_running_loop().time()
            # Chain management for the round-5 thinking indicator.
            # ``is_reasoning_event`` gates whether we extend the
            # chain (set ``thinking_since`` on the first reasoning
            # event of a chain) or break it (clear ``thinking_since``
            # + reset ``thinking_indicator_sent`` so a fresh chain
            # is eligible to send again). Lifecycle events are
            # non-reasoning so they break the chain too. A
            # malformed JSON payload (``event_obj is None``) is
            # treated as "unknown, bail out" and breaks the chain.
            is_reasoning_event = (
                isinstance(event_obj, dict)
                and self._is_reasoning_event(event_obj)
            )
            if is_reasoning_event:
                if thinking_since is None:
                    thinking_since = now
            else:
                if thinking_since is not None:
                    thinking_since = None
                    thinking_indicator_sent = False
            # Round-6 tool-pulse arm/disarm. A long tool call leaves
            # the placeholder sitting on the one-line indicator that
            # round 2 shipped; this arms a periodic "still working"
            # pulse that updates the indicator in place with the
            # elapsed seconds. The arm fires on the first
            # ``item.started`` for a tool call; the disarm is the
            # matching ``item.completed`` for the same name so a
            # stale complete from a prior call cannot wipe a fresh
            # arm. The arm/ disarm is intentionally outside the
            # prose gate so the tool-call pulse fires even when the
            # prose "growing_ok" rule would otherwise dedup.
            if self._is_tool_call_start_event(event_obj):
                tool_name = self._tool_call_name(event_obj)
                if tool_name is not None:
                    pending_tool_name = tool_name
                    pending_tool_since = now
                    last_pulse_at = None
            elif self._is_tool_call_complete_event(event_obj):
                completed_name = self._tool_call_name(event_obj)
                if completed_name is not None and completed_name == pending_tool_name:
                    pending_tool_name = None
                    pending_tool_since = None
                    last_pulse_at = None
            is_prose = isinstance(event_obj, dict) and self._is_prose_event(event_obj)
            is_prose_complete = is_prose and event_obj.get("type") == "item.completed"
            # Tool calls (is_prose False) bypass the gate: the
            # user wants the current "🔧 name..." state, not a
            # growing sequence. Lifecycle events are already
            # filtered to "" by _event_summary so they cannot
            # reach this point.
            growing_ok = (
                not is_prose
                or last_prose_text is None
                or len(event_text) > len(last_prose_text)
                or is_prose_complete
            )
            # Round-5 thinking indicator send: emit "💭 thinking..."
            # once per chain after the threshold. Shares the
            # existing ``telegram_progress_seconds`` cooldown so
            # the next prose is not double-blasted. Sent BEFORE
            # the prose ``if`` so the cooldown clock is shared:
            # if this came after, the prose block would have
            # already updated ``last_sent`` and the indicator
            # would not fire. ``last_sent_text`` is set to the
            # indicator so a hypothetical back-to-back duplicate
            # is deduped; in practice a non-reasoning event resets
            # the chain before the dedup is reached.
            if (
                event_text
                and event_text != last_sent_text
                and self._should_send_event_progress(event_text, event_obj)
                and (prose_allowed or not self._is_prose_event_text(event_text))
                and now - last_sent >= self.settings.telegram_progress_seconds
                and growing_ok
            ):
                last_sent = now
                last_sent_text = event_text
                if is_prose:
                    last_prose_text = None if is_prose_complete else event_text
                await on_progress(truncate(event_text, 1200))
            # Round-5 thinking indicator (kept identical to the prior
            # version except the mode gate). ``quiet`` drops it
            # entirely; ``compact``/``verbose`` keep it.
            if (
                progress_mode != "quiet"
                and thinking_since is not None
                and not thinking_indicator_sent
                and now - thinking_since >= THINKING_THRESHOLD_SECONDS
                and now - last_sent >= self.settings.telegram_progress_seconds
            ):
                last_sent = now
                last_sent_text = THINKING_INDICATOR
                thinking_indicator_sent = True
                await on_progress(truncate(THINKING_INDICATOR, 1200))
            # Round-6 tool-pulse send. Only fires after
            # ``TOOL_PULSE_THRESHOLD_SECONDS`` of an active tool call
            # and re-arms every ``TOOL_PULSE_INTERVAL_SECONDS`` until
            # the matching ``item.completed`` disarms. Shares the
            # ``telegram_progress_seconds`` cooldown with the rest
            # of the gate ladder so the pulse never overruns
            # Telegram's 20 edits/min/message limit. ``last_sent_text``
            # is not updated on the pulse so a tool-call summary
            # that lands right after the pulse still surfaces (the
            # prose gate's growing_ok / event_text != last_sent_text
            # rules apply normally). In ``quiet`` mode the pulse is
            # suppressed (final summary is the only user-facing
            # intermediate); in ``verbose`` it works as before.
            if (
                progress_mode != "quiet"
                and pending_tool_name is not None
                and pending_tool_since is not None
                and now - pending_tool_since >= TOOL_PULSE_THRESHOLD_SECONDS
                and (last_pulse_at is None or now - last_pulse_at >= TOOL_PULSE_INTERVAL_SECONDS)
                and now - last_sent >= self.settings.telegram_progress_seconds
            ):
                last_sent = now
                last_pulse_at = now
                elapsed = int(now - pending_tool_since)
                await on_progress(f"\U0001f527 {pending_tool_name} ({elapsed}s)...")


async def _read_stderr(self, job: Job, process: asyncio.subprocess.Process) -> None:
    assert process.stderr is not None
    chunks: list[str] = []
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        chunks.append(line.decode("utf-8", errors="replace"))
    if chunks and not job.error:
        job.error = truncate("".join(chunks), 3000)
    if job.return_code is not None and job.return_code < 0:
        job.error = "cancelled"


def _capture_usage(self, job: Job, raw_line: str) -> None:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return
    if event.get("type") != "turn.completed":
        return
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return
    job.usage = {key: int(value) for key, value in usage.items() if isinstance(value, int)}
    self._write_job_metadata(job)


def _event_summary(self, raw_line: str) -> str:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return truncate(raw_line, 1000)

    if self._is_reasoning_event(event):
        return ""

    event_type = str(event.get("type") or event.get("event") or "").lower()
    # Lifecycle events: drop them on the floor. ``turn.completed``'s
    # usage payload is captured separately by ``_capture_usage`` so
    # suppressing the summary here loses no data. Returning "" also
    # keeps ``_read_jsonl_stdout``'s cooldown clock from being reset
    # by a JSON dump, which would otherwise hold the placeholder
    # hostage to no-op updates.
    if event_type in self._LIFECYCLE_EVENT_TYPES:
        return ""

    # Top-level text-like fields. No ``event_type:`` prefix: the
    # user wants the chat surface to read like a chat, not like a
    # raw event log.
    for key in ("message", "summary", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    agent_text = self._agent_message_text(event)
    if agent_text is not None:
        return agent_text

    # Tool-call indicator: short, so the next prose edit can replace
    # it without the user feeling like they lost information. The
    # type filter now includes ``command_execution`` (shell calls),
    # which never carry a name and so fall back to ``"shell"``.
    tool_name = self._tool_call_name(event)
    if tool_name is not None:
        return f"🔧 {tool_name}..."

    # Opaque event (item with no agent_message text, no tool name,
    # no top-level text field). Return "" instead of dumping JSON:
    # the raw line is still captured in ``job.log_path`` for
    # debugging, and the chat surface stays clean.
    return ""


THINKING_INDICATOR = "💭 thinking..."


THINKING_THRESHOLD_SECONDS = 1.0


TOOL_PULSE_THRESHOLD_SECONDS = 4.0


TOOL_PULSE_INTERVAL_SECONDS = 4.0


# Progress verbosity policy. ``verbose`` forwards every event
# category to the chat (current behavior); ``compact`` (default)
# forwards only tool indicators, the thinking indicator, and tool
# pulses — not the agent's streaming prose; ``quiet`` forwards
# nothing intermediate. ``is_prose_event`` is the only event
# classification the streaming layer needs: tool calls, the
# thinking indicator, and tool pulses are already a different
# code path that does not go through this gate.
def _is_prose_event_text(self, event_text: str) -> bool:
    """True when the event_text we are about to forward is a chunk
    of agent prose (top-level message / summary / text / delta or
    ``agent_message`` text) rather than a tool indicator, thinking
    indicator, or tool pulse.

    Used by the ``CONVEYOR_PROGRESS_MODE`` filter in
    ``_read_jsonl_stdout`` to drop prose in compact/quiet modes.
    A leading ``🔧`` means a tool indicator; ``💭`` means the
    thinking indicator; any other text from ``_event_summary`` is
    prose.
    """
    if not event_text:
        return False
    if event_text.startswith("🔧") or event_text.startswith("\U0001f527"):
        return False
    if event_text.startswith("💭") or event_text.startswith("\U0001f4ad"):
        return False
    return True


def _progress_mode_allows_prose(mode: str) -> bool:
    return mode == "verbose"

# Abort codex when the provider keeps reconnecting (high demand). Without
# this, the subprocess can hold the single-job lock until CODEX_TIMEOUT_SECONDS.
RECONNECT_STALL_LIMIT = 5
