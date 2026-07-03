"""runner/operators/run.py — split out of runner.py.

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
from uuid import uuid4


# Project root for codex --add-dir and CODEX_RUNNER_HOME env.
from runner._paths import RUNNER_HOME
from runner.file_lock import file_lock
from runner.types import Job, JobMode, JobState, ProgressCallback
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text

def _extract_command_name(command: str) -> str | None:
    """Pull a short human-readable executable name out of a codex
    ``command_execution.command`` string. The real-world shape is
    ``/bin/bash -lc 'cmd1 && cmd2 | cmd3'``; the round-1 contract was
    to just say "shell", but the user wants to know what is actually
    running. We strip the bash wrapper, take the last ``&&`` segment
    (the effective command in a chain), then the first part of any
    ``|`` pipe (the source), then the first whitespace-delimited word,
    and finally strip the path with ``Path.name``. Returns None when
    nothing clean comes out (e.g. empty command, no shell metachars
    and no leading executable); the caller falls back to ``"shell"``.
    """
    if not command or not command.strip():
        return None
    cmd = command.strip()
    # Strip the bash -lc '...' wrapper if present. The opening and
    # closing quote are the same character; we look for either pair.
    m = re.match(r"^/bin/(?:ba)?sh\s+-lc\s+([\'\"])(.*)\1\s*$", cmd, re.DOTALL)
    if m:
        cmd = m.group(2)
    # Last `&&` segment is usually the effective command in a chain
    # (e.g. `cd ... && .venv/bin/python -m runner ...` -> python).
    if "&&" in cmd:
        cmd = cmd.rsplit("&&", 1)[-1]
    # If piped, take the first part (the source of the pipe).
    if "|" in cmd:
        cmd = cmd.split("|", 1)[0]
    # First whitespace-delimited word, strip surrounding quotes.
    first = re.split(r"\s+", cmd.strip(), 1)[0].strip("\'\"")
    if not first:
        return None
    # Strip the path; `Path("ls").name == "ls"`, `Path(".venv/bin/python").name == "python"`.
    name = Path(first).name
    if not name or not name.strip():
        return None
    return name[:32]

# ---- Thinking indicator (chat-feel round 5) -----------------------------
# Reasoning events stream silently via _event_summary returning "", so the
# placeholder sits at "⏳ Got it, working on it..." for 5-30s during a hard
# think and the chat feels frozen. After THINKING_THRESHOLD_SECONDS of
# sustained reasoning, surface a short indicator to the placeholder so the
# user knows the model is alive. Shared cooldown with the existing
# telegram_progress_seconds so the next prose is not double-blasted. Sent at
# most once per chain; any non-reasoning event (prose, tool indicator,
# item.completed, lifecycle, malformed JSON) ends the chain so the next
# reasoning burst starts a fresh threshold window. Re-binding
# THINKING_THRESHOLD_SECONDS at the module level is the test override hook
# (mirrors the frozen-Settings bypass used by progress_smoke).
THINKING_INDICATOR = "💭 thinking..."
THINKING_THRESHOLD_SECONDS = 1.0
# ---- Tool-call pulse (chat-feel round 6) -----------------------------
# A long tool call (network fetch, big shell pipeline) leaves the
# placeholder sitting on the one-line "🔧 name..." indicator that
# round 2 shipped, with no further edits for 5-30s. The user reads
# the chat as frozen. This block adds a periodic "still working"
# pulse that updates the indicator in place with the elapsed seconds.
# The arm fires on the first item.started for a tool call; the disarm
# is the matching item.completed for the same name. The pulse shares
# the telegram_progress_seconds cooldown so we never overrun Telegram's
# 20 edits/min/message limit. Re-binding TOOL_PULSE_THRESHOLD_SECONDS
# and TOOL_PULSE_INTERVAL_SECONDS at the module level is the test
# override hook (mirrors the THINKING_THRESHOLD_SECONDS pattern above
# and the frozen-Settings bypass used by progress_smoke).
TOOL_PULSE_THRESHOLD_SECONDS = 4.0
TOOL_PULSE_INTERVAL_SECONDS = 4.0
MEMO_ENV_SKIP_DIRS = (".venv", "venv", "env", "node_modules", "__pycache__")

async def validate(self) -> None:
    root = self.settings.codex_workspace_root
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"CODEX_WORKSPACE_ROOT does not exist or is not a directory: {root}")
    await self._git(["rev-parse", "--is-inside-work-tree"], cwd=root)
    top = (await self._git(["rev-parse", "--show-toplevel"], cwd=root)).strip()
    if Path(top).resolve() != root:
        raise RuntimeError(f"CODEX_WORKSPACE_ROOT must be the git repo root: expected {top}")
    self.settings.codex_task_root.mkdir(parents=True, exist_ok=True)
    (self.settings.codex_task_root / "logs").mkdir(parents=True, exist_ok=True)
    (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)


async def start(
    self,
    mode: JobMode,
    prompt: str,
    on_progress: ProgressCallback,
) -> Job:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt is empty.")
    if len(prompt) > 8000:
        raise ValueError("Prompt is too long; keep it under 8000 characters.")

    async with self._lock:
        if self.current_job and self.current_job.state == JobState.RUNNING:
            raise RuntimeError(f"Job {self.current_job.id} is already running.")
        
        lock_path = self.settings.codex_task_root / "locks" / "run.lock"
        with file_lock(lock_path):
            # Check other running jobs across processes
            for record in self.job_records(100):
                if record.state == "running":
                    raise RuntimeError(f"Job {record.id} is already running.")

            sandbox = mode.sandbox
            job = Job(id=self._new_job_id(), mode=mode, prompt=prompt, sandbox=sandbox)
            job.max_attempts = 1 + len(self.settings.codex_retry_429_delays_seconds)
            
            # Write initial metadata synchronously so other processes see it immediately
            logs_dir = self.settings.codex_task_root / "logs" / job.id
            logs_dir.mkdir(parents=True, exist_ok=True)
            job.metadata_path = logs_dir / "job.json"
            self._write_job_metadata(job)
            
            self.current_job = job
            self.last_job = job
            
        asyncio.create_task(self._run_job(job, on_progress))
        return job


async def cancel(self) -> str:
    job = self.current_job
    if not job or job.state != JobState.RUNNING:
        return "No running job."
    job.cancel_requested = True
    job.last_event = "cancelling"
    if not job.process:
        return f"Cancellation requested for job {job.id}."
    job.process.terminate()
    try:
        await asyncio.wait_for(job.process.wait(), timeout=10)
    except asyncio.TimeoutError:
        job.process.kill()
    return f"Cancellation requested for job {job.id}."


async def _run_job(self, job: Job, on_progress: ProgressCallback) -> None:
    try:
        await self.validate()
        
        lock_path = self.settings.codex_task_root / "locks" / "run.lock"
        with file_lock(lock_path):
            job.worktree_path = await self._create_worktree(job)
            self._write_job_metadata(job)
        if job.cancel_requested:
            job.error = "cancelled"
            job.state = JobState.CANCELLED
            self._write_job_metadata(job)
            await on_progress(f"Cancelled job {job.id}.")
            return

        delays = self.settings.codex_retry_429_delays_seconds
        for attempt_index in range(job.max_attempts):
            job.attempt = attempt_index + 1
            job.error = ""
            job.return_code = None
            job.process = None
            job.rate_limited = False
            job.log_path = logs_dir / f"attempt-{job.attempt}.jsonl"
            job.final_message_path = logs_dir / f"attempt-{job.attempt}-final.txt"
            job.last_event = f"starting attempt {job.attempt}/{job.max_attempts}"
            self._write_job_metadata(job)

            await self._run_codex_attempt(job, on_progress)
            job.rate_limited = self._is_rate_limited(job)
            self._write_job_metadata(job)

            if job.return_code == 0 and not job.error:
                job.state = JobState.COMPLETED
                job.summary = self._read_final_message(job)
                self._write_job_metadata(job)
                await on_progress(self._completed_message(job))
                return
            if job.error == "cancelled" or job.cancel_requested:
                job.state = JobState.CANCELLED
                self._write_job_metadata(job)
                await on_progress(f"Cancelled job {job.id}.")
                return
            if not self._is_rate_limited(job) or attempt_index >= len(delays):
                job.state = JobState.FAILED
                self._write_job_metadata(job)
                await on_progress(self._failed_message(job))
                return

            delay = delays[attempt_index]
            job.last_event = f"rate limited; retrying attempt {job.attempt + 1}/{job.max_attempts} in {delay}s"
            job.rate_limited = True
            self._write_job_metadata(job)
            await on_progress(f"MiniMax 现在限流，我会在 {delay}s 后自动重试。")
            try:
                await asyncio.wait_for(self._wait_until_cancelled(job), timeout=delay)
                job.error = "cancelled"
                job.state = JobState.CANCELLED
                self._write_job_metadata(job)
                await on_progress(f"Cancelled job {job.id}.")
                return
            except asyncio.TimeoutError:
                continue
    except Exception as exc:
        job.state = JobState.FAILED
        redacted_exc = redact_text(str(exc))
        job.error = redacted_exc
        self._write_job_metadata(job)
        await on_progress(f"这次没跑成：{truncate(redacted_exc, 2500)}")
    finally:
        job.finished_at = datetime.now(timezone.utc)
        self._write_job_metadata(job)
        if self.current_job and self.current_job.id == job.id:
            self.current_job = None


async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
    command = self._codex_command(job)
    env = self._child_env()

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=job.worktree_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    job.process = process
    assert process.stdin is not None
    payload = (job.mode.stdin_prefix + self._prefetch_memory(job) + job.prompt).encode("utf-8")
    process.stdin.write(payload)
    await process.stdin.drain()
    process.stdin.close()

    stdout_task = asyncio.create_task(self._read_jsonl_stdout(job, process, on_progress))
    stderr_task = asyncio.create_task(self._read_stderr(job, process))
    try:
        job.return_code = await asyncio.wait_for(process.wait(), timeout=self.settings.codex_timeout_seconds)
    except asyncio.TimeoutError:
        job.error = f"Timed out after {self.settings.codex_timeout_seconds} seconds."
        process.kill()
        job.return_code = await process.wait()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    job.process = None


async def _wait_until_cancelled(self, job: Job) -> None:
    while not job.cancel_requested:
        await asyncio.sleep(1)


def _is_rate_limited(self, job: Job) -> bool:
    text = f"{job.error}\n{job.last_event}".lower()
    return "429" in text or "too many requests" in text or "rate limit" in text or "high demand" in text


def _should_send_event_progress(
    self, event_text: str, event_obj: dict | None = None,
) -> bool:
    """Forward codex events to the user's progress callback when they
    have user-readable text. Conservative: skip raw tool-call JSON,
    lifecycle events, and anything that isn't a top-level text-like
    field. The final answer reaches the user via --output-last-message
    regardless, so this stream is decorative.
    """
    return self._is_user_visible_event(event_obj)


def _is_user_visible_event(self, event_obj: dict | None) -> bool:
    """True when the event has a top-level text-like field that would
    be worth showing in chat. Returns False for non-dict payloads,
    reasoning events, and any event whose only "text" lives in a
    raw tool-call/item/data block (which is JSON, not chat text).
    """
    if not isinstance(event_obj, dict):
        return False
    if self._is_reasoning_event(event_obj):
        return False
    for key in ("message", "summary", "text", "delta"):
        value = event_obj.get(key)
        if isinstance(value, str) and value.strip():
            return True
    # Codex nests the model's prose under item.text for agent_message
    # items. Surface it so the user sees the answer stream in place
    # instead of waiting for the final --output-last-message dump.
    if self._agent_message_text(event_obj) is not None:
        return True
    # Tool-call indicator: function_call items show up empty in the
    # top-level text fields but the user wants to see the model is
    # actually doing something during a long tool invocation
    # (otherwise the placeholder sits still for 5-30s and looks
    # frozen). The summary line is "🔧 name...".
    if self._tool_call_name(event_obj) is not None:
        return True
    return False


def _is_prose_event(self, event_obj: dict | None) -> bool:
    """True when the event carries user-readable chat prose that
    should be streamed growing in place. Strict subset of
    ``_is_user_visible_event`` (same checks minus the tool-call
    indicator branch). Tool calls are short-lived indicators
    and the user wants the *current* state, not a growing
    sequence, so they are excluded here on purpose.
    """
    if not isinstance(event_obj, dict):
        return False
    if self._is_reasoning_event(event_obj):
        return False
    # Agent_message items are the streaming prose. Check this
    # BEFORE the top-level text sweep so an item envelope wins
    # over any incidental top-level field on the same event.
    if self._agent_message_text(event_obj) is not None:
        return True
    for key in ("message", "summary", "text", "delta"):
        value = event_obj.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _tool_call_name(self, event_obj: dict | None) -> str | None:
    """Return the tool name for a function_call / tool_call item, or
    None when this event isn't a tool invocation. Used by
    ``_is_user_visible_event`` and ``_event_summary`` to surface a
    short "🔧 name..." progress line so the user knows the model is
    doing work between prose events.

    ``command_execution`` items are shell invocations; codex does not
    set a ``name`` on them, so we fall back to ``"shell"`` so the user
    sees a short indicator instead of a multi-kilobyte curl command
    being JSON-dumped into the chat.
    """
    if not isinstance(event_obj, dict):
        return None
    item = event_obj.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "").lower()
    if not any(tag in item_type for tag in ("function_call", "tool_call", "command_execution")):
        return None
    name = item.get("name") or item.get("tool") or item.get("function")
    if isinstance(name, str) and name.strip():
        return name.strip()
    # command_execution items never carry a name; surface them as
    # "shell" so the progress line is informative. function_call /
    # tool_call without a name still return None to avoid a vague
    # "🔧 ..." indicator (the original round-1 contract).
    if "command_execution" in item_type:
        command = item.get("command")
        if isinstance(command, str) and command.strip():
            extracted = _extract_command_name(command)
            if extracted:
                return extracted
        return "shell"
    return None


def _agent_message_text(self, event_obj: dict | None) -> str | None:
    """Extract the streaming prose from a codex item envelope.

    Codex's --json stream puts the model's reply under
    ``item.text`` for ``type == "agent_message"`` items (item.updated
    during streaming, item.completed at the end). Top-level text-like
    fields are surfaced by ``_is_user_visible_event``; this helper
    is for the nested case so the user actually sees the answer text
    grow in place instead of waiting for ``--output-last-message``.
    """
    if not isinstance(event_obj, dict):
        return None
    item = event_obj.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "").lower()
    if "agent_message" not in item_type:
        return None
    item_text = item.get("text")
    if isinstance(item_text, str) and item_text.strip():
        return item_text.strip()
    return None


def _completed_message(self, job: Job) -> str:
    summary = truncate(job.summary or job.last_event, 3000)
    if not summary:
        return "（这次没有生成回复）"
    return summary


def _failed_message(self, job: Job) -> str:
    if self._is_rate_limited(job):
        return "现在有点忙，稍等一会儿再发我试试。"
    text = job.error or job.last_event
    return f"出错了：{truncate(text, 2500)}"


def _codex_command(self, job: Job) -> list[str]:
    command = [
        self.settings.codex_bin,
        "exec",
        "--json",
        "--sandbox",
        job.sandbox,
        "--cd",
        str(job.worktree_path),
        "--add-dir",
        str(RUNNER_HOME),
        "--output-last-message",
        str(job.final_message_path),
        "-",
    ]
    if self.settings.codex_model:
        command[2:2] = ["--model", self.settings.codex_model]
    return command


def _child_env(self) -> dict[str, str]:
    from security.secrets import child_env_from
    env = child_env_from(os.environ)
    env["CODEX_TELEGRAM_JOB"] = "1"
    env["CODEX_RUNNER_HOME"] = str(RUNNER_HOME)
    return env


def _new_job_id(self) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = re.sub("[^a-z0-9]", "", uuid4().hex[:8].lower())
    return f"{stamp}-{suffix}"


def _elapsed(self, job: Job) -> str:
    end = job.finished_at or datetime.now(timezone.utc)
    seconds = int((end - job.started_at).total_seconds())
    return f"{seconds // 60}m {seconds % 60}s"
