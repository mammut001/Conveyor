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

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from channel.types import InboundMessage, OutboundPort
from redaction import truncate, redact_text
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


# Hourly rate limiting tracking
JOB_SUBMISSION_TIMESTAMPS: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(operator_id: str, max_per_hour: int) -> bool:
    """Return True if rate limit is exceeded, False otherwise."""
    now = time.time()
    one_hour_ago = now - 3600
    timestamps = JOB_SUBMISSION_TIMESTAMPS[operator_id]
    
    # Filter out timestamps older than 1 hour
    active_timestamps = [t for t in timestamps if t > one_hour_ago]
    JOB_SUBMISSION_TIMESTAMPS[operator_id] = active_timestamps
    
    if len(active_timestamps) >= max_per_hour:
        return True
    return False


def record_job_submission(operator_id: str) -> None:
    JOB_SUBMISSION_TIMESTAMPS[operator_id].append(time.time())


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


_JOB_TASKS: set[asyncio.Task] = set()


def _spawn_execution(execution, queue_job_id, msg, port):
    async def run():
        try:
            await execution
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Background Codex task failed: %s", queue_job_id)
            from handlers.job_queue import get_job_queue
            await get_job_queue().mark_running_failed(str(exc), queue_job_id)
            await port.reply(msg, "任务执行异常，已记录失败并释放队列。请用 /status 查看。")
    task = asyncio.create_task(run())
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)
    return task


async def handle_codex_job(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    mode: JobMode = JobMode.RUN,
    prompt: str | None = None,
    *,
    wait: bool | None = None,
) -> None:
    if wait is None:
        wait = getattr(port, "wait_for_job", True)
    await submit_codex_job(msg, port, runner, mode=mode, prompt=prompt, wait=wait)


async def submit_codex_job(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    mode: JobMode = JobMode.RUN,
    prompt: str | None = None,
    *,
    wait: bool = False,
) -> tuple[bool, str, "QueuedJob | None"]:
    """Submit a job through the shared persistent queue.

    Browser requests return immediately after enqueue (``wait=False``), while
    existing chat adapters retain their wait-for-final-response behavior.
    """
    body = (prompt if prompt is not None else msg.text).strip()
    if not body:
        await port.reply(msg, "Usage: /run <prompt>")
        return False, "empty prompt", None

    operator_id = msg.operator_id or "unknown"
    if check_rate_limit(operator_id, runner.settings.conveyor_max_jobs_per_hour):
        await port.reply(msg, "提交失败：已达到每小时最大任务数限制")
        return False, "rate limit", None

    # P3.8: Check if a job is already running and queue if needed
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()

    # Check queue limit before attempting to enqueue
    if queue.queue_length >= runner.settings.conveyor_max_pending_jobs:
        await port.reply(msg, f"无法排队：队列已满（最多 {runner.settings.conveyor_max_pending_jobs} 个任务）")
        return False, "queue full", None

    record_job_submission(operator_id)
    success, queue_msg, queued_job = await queue.enqueue(
        mode=mode.value,
        prompt=body,
        msg=msg,
        port=port,
        runner=runner,
        original_text=msg.text,
    )
    if not success:
        await port.reply(msg, f"无法排队：{queue_msg}")
        return False, queue_msg, None

    if hasattr(port, "on_job_submitted"):
        port.on_job_submitted(queued_job)

    # SQLite is authoritative because Telegram, Feishu and Web can be
    # separate processes sharing one queue.
    if queue.has_running_job and not queue.can_start(queued_job.id):
        # Its lane is busy (or the parallel limit is reached): it stays queued.
        await port.reply(msg, queue_msg)
        return True, queue_msg, queued_job

    # No job running, execute immediately
    dequeued_job = await queue.dequeue(require_idle=True)
    if dequeued_job:
        execute_msg = dequeued_job._msg or msg
        execute_port = dequeued_job._port or port
        execute_mode = JobMode.FIX if dequeued_job.mode == "fix" else JobMode.RUN
        execution = _execute_codex_job(
            execute_msg, execute_port, runner, execute_mode,
            dequeued_job.prompt, queue_job_id=dequeued_job.id,
        )
        if wait:
            await execution
        else:
            _spawn_execution(execution, dequeued_job.id, execute_msg, execute_port)
    return True, queue_msg, queued_job


class _JobIdentityPort:
    """Keep asynchronous job output identifiable after an agent switch."""
    def __init__(self, port, label):
        self._port = port
        self._label = label

    def __getattr__(self, name):
        return getattr(self._port, name)

    def _text(self, text):
        return f"{self._label}\n{text}"

    async def reply(self, msg, text):
        return await self._port.reply(msg, self._text(text))

    async def send_new(self, msg, text):
        return await self._port.send_new(msg, self._text(text))

    async def edit_progress(self, msg, placeholder, text):
        return await self._port.edit_progress(msg, placeholder, self._text(text))


async def _execute_codex_job(
    msg: InboundMessage,
    port: OutboundPort,
    runner: CodexRunner,
    mode: JobMode,
    body: str,
    *,
    queue_job_id: str | None = None,
) -> None:
    """Execute a Codex job directly (not through queue)."""
    # Each lane has its own runner, so jobs of different agents can overlap
    # and "the current job" means this conversation's.
    from job_lanes import runner_for_chat
    runner = runner_for_chat(runner, getattr(runner, "settings", None), msg.channel, msg.chat_id)
    # Session context injection: prepend recent turns so the LLM has
    # continuity when the user says "继续" / "continue". Only for LLM
    # jobs (handle_codex_job is only called for /run, /fix, and free
    # text fallback — never for deterministic commands).
    from handlers.session import build_context_prompt, append_turn
    from agents import AgentError, agent_for_chat, instructions_for_chat, profile_block, workspace_for_chat
    try:
        ctx_prompt = (
            profile_block(*instructions_for_chat(runner.settings, msg.channel, msg.chat_id))
            + build_context_prompt(runner.settings, msg)
        )
        agent_workspace = workspace_for_chat(runner.settings, msg.channel, msg.chat_id)
        agent = agent_for_chat(runner.settings, msg.channel, msg.chat_id)
    except AgentError as exc:
        from handlers.job_queue import get_job_queue
        await get_job_queue().mark_running_failed(str(exc), queue_job_id)
        await port.reply(msg, str(exc))
        return
    from handlers.workers import PhysicalOriginPort
    if agent and (
        isinstance(port, PhysicalOriginPort)
        or (msg.channel == "telegram" and ":agent:" in msg.chat_id)
    ):
        label = agent["name"]
        try:
            from transcript_store import session_identity
            from worker_sessions import WorkerSessionStore
            owned = WorkerSessionStore(runner.settings).get(
                session_identity(msg.channel, msg.chat_id, msg.operator_id)
            )
            if owned and owned.get("title"):
                label = f"{label} · {owned['title']}"
        except Exception:
            pass
        port = _JobIdentityPort(port, f"{label} · {queue_job_id or 'Codex'}")
    user_text_for_session = body  # remember for session recording

    progress_mode = _normalize_mode(getattr(runner.settings, "conveyor_progress_mode", "compact"))

    if queue_job_id:
        try:
            from agent_events import emit_event
            emit_event(
                runner.settings, "assistant.started", queue_job_id, {},
                session_id=msg.chat_id,
            )
        except Exception:
            logger.exception("Could not persist assistant.started")

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
        if queue_job_id:
            try:
                from agent_events import emit_event
                emit_event(
                    runner.settings, "assistant.delta", queue_job_id,
                    {"text": outgoing}, session_id=msg.chat_id,
                )
            except Exception:
                logger.exception("Could not persist assistant progress")
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

    # An agent with its own project folder works there. Passed only when set,
    # so callers and fakes that predate agents see the same call as before.
    start_options = {"workspace_root": agent_workspace} if agent_workspace is not None else {}
    try:
        job = await runner.start(mode, effective_body, progress, **start_options)
    except Exception as exc:
        # Failure to even start the job (e.g. invalid args, Codex
        # missing). Mark running queue row as failed.
        from handlers.job_queue import get_job_queue
        await get_job_queue().mark_running_failed(str(exc), queue_job_id)

        # On Feishu, surface this as a card; Telegram keeps
        # the existing text path.
        redacted_exc = redact_text(str(exc))
        if msg.channel == "feishu" and hasattr(port, "send_card"):
            try:
                from channel.feishu_cards import job_failed_card
                await port.send_card(msg, job_failed_card(
                    job_id="(start-failed)",
                    error=f"现在不能开始：{truncate(redacted_exc, 1200)}",
                ))
                return
            except Exception:
                pass
        await port.reply(msg, f"现在不能开始：{truncate(redacted_exc, 1200)}")
        return

    if queue_job_id:
        job.external_id = queue_job_id
        from handlers.job_queue import get_job_queue
        get_job_queue().bind_runtime_job(queue_job_id, job)

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
        err_truncated = truncate(redact_text(job.error), 3500)
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

    # Bridge job summary to Flash chat memory so follow-up conversations have full context.
    try:
        from handlers.chat import chat_key, remember
        key = chat_key(msg)
        status_label = "完成" if not job.error else "失败"
        user_turn_text = f"[任务指令] {truncate(user_text_for_session, 200)}"
        assistant_turn_text = f"[Codex 任务 {job.id} {status_label}]\n{truncate(final_answer, 800)}"
        remember(
            key,
            user_turn_text,
            assistant_turn_text,
            getattr(runner.settings, "chat_history_turns", 6),
            settings=runner.settings,
        )
    except Exception:
        logger.debug("Failed to bridge job summary to chat memory", exc_info=True)

    if queue_job_id:
        try:
            from agent_events import emit_event
            emit_event(
                runner.settings,
                "assistant.completed" if not job.error else "assistant.failed",
                queue_job_id,
                {"text": final_answer, "runtime_job_id": job.id},
                session_id=msg.chat_id,
            )
        except Exception:
            logger.exception("Could not persist assistant terminal event")

    # P3.8: Notify queue that job completed, so next queued job can start
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    await queue.on_job_completed(
        job.id,
        queue_job_id=queue_job_id,
        final_state=getattr(job.state, "value", str(job.state)),
        error=job.error or None,
    )


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _feishu_http_text(settings: "Settings", chat_id: str, text: str) -> bool:
    """Send a recovered Feishu message with the app credentials over HTTP.

    Does not read or log the secret. Works from the Web process, which does
    not hold the Feishu bot's in-process channel.
    """
    import json
    import urllib.request
    app_id = getattr(settings, "lark_app_id", None)
    app_secret = getattr(settings, "lark_app_secret", None)
    if not app_id or not app_secret or not chat_id:
        return False
    token_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            tenant = json.loads(resp.read().decode("utf-8")).get("tenant_access_token")
    except Exception:
        logger.warning("Recovered Feishu delivery could not obtain a tenant token")
        return False
    if not tenant:
        return False
    payload = json.dumps({
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }).encode("utf-8")
    send_req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tenant}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(send_req, timeout=10) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        logger.warning("Recovered Feishu delivery failed")
        return False


class RecoveredOutboundPort:
    """OutboundPort that sends messages back to the operator for recovered jobs after a process restart."""
    supports_inline_buttons = False
    supports_attachments = False

    def __init__(self, channel: str, chat_id: str, settings: "Settings") -> None:
        self.channel = channel
        self.chat_id = chat_id
        self.settings = settings

    async def reply(self, msg: InboundMessage, text: str) -> str | None:
        return await self.send_new(msg, text)

    async def send_new(self, msg: InboundMessage, text: str) -> str | None:
        if self.channel == "telegram":
            from scripts.telegram_api import send_message
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, send_message, self.settings, text, self.chat_id)
                return "recovered-msg-id"
            except Exception:
                logger.exception("Failed to send recovered telegram message")
                return None
        elif self.channel == "feishu":
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                ok = await loop.run_in_executor(None, _feishu_http_text, self.settings, self.chat_id, text)
                if ok:
                    return "recovered-msg-id"
            except Exception:
                logger.warning("Recovered Feishu delivery failed")
        return None

    async def edit_progress(self, msg: InboundMessage, placeholder_id: Any, text: str) -> bool:
        return False

    async def send_card(self, msg: InboundMessage, card: dict, *, reply_to: str | None = None) -> str | None:
        if self.channel == "feishu":
            try:
                from feishu_bot import _get_channel
                feishu_channel = _get_channel()
                if feishu_channel:
                    from channel.feishu import FeishuOutbound
                    port = FeishuOutbound(feishu_channel)
                    return await port.send_card(msg, card, reply_to=reply_to)
            except Exception:
                logger.exception("Failed to send recovered feishu card")
        from channel.feishu_cards import flatten_card_to_text
        text = flatten_card_to_text(card)
        return await self.send_new(msg, text)


async def _start_queued_job_callback(queued_job: QueuedJob) -> None:
    import asyncio
    from handlers.job_queue import get_job_queue
    queue = get_job_queue()
    runner = queued_job._runner or queue._runner
    settings = runner.settings if runner else queue._settings
    
    if runner is None or settings is None:
        logger.error("Runner or settings not configured in queue, cannot start queued job %s", queued_job.id)
        return
        
    msg = queued_job._msg
    if msg is None:
        from channel.types import InboundMessage
        msg = InboundMessage(
            channel=queued_job.channel,
            operator_id=queued_job.operator_id,
            chat_id=queued_job.chat_id,
            message_id=None,
            text=queued_job.prompt,
        )

    port = queued_job._port
    if port is None:
        origin = queued_job.delivery_origin or {}
        port = RecoveredOutboundPort(
            str(origin.get("channel") or queued_job.channel),
            str(origin.get("chat_id") or queued_job.chat_id),
            settings,
        )
        
    mode = JobMode.FIX if queued_job.mode == "fix" else JobMode.RUN
    
    _spawn_execution(_execute_codex_job(
        msg, port, runner, mode, queued_job.prompt, queue_job_id=queued_job.id,
    ), queued_job.id, msg, port)

from handlers.job_queue import get_job_queue
get_job_queue().set_start_callback(_start_queued_job_callback)
