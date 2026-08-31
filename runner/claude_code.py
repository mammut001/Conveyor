"""Experimental Claude Code execution backend.

This backend reuses Conveyor's existing job/worktree/apply lifecycle and only
replaces the provider attempt.  Claude Code runs locally in non-interactive
print mode using the user's existing Claude Code authentication.

It is intentionally not selected by default.  No API keys are read or stored
by Conveyor.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_events import emit_event
from redaction import redact_text, truncate
from runner.core import CodexRunner
from runner.types import Job, JobMode, ProgressCallback


MAX_CLAUDE_STREAM_LINE = 1_000_000
MAX_CLAUDE_LOG_BYTES = 2_000_000
MAX_CLAUDE_STDERR_BYTES = 32_000


@dataclass(frozen=True)
class _ClaudeEvent:
    progress: str = ""
    result: str = ""
    error: str = ""
    kind: str = ""
    payload: dict[str, Any] | None = None
    tool_call_id: str | None = None


class ClaudeCodeBackend(CodexRunner):
    """CodexRunner-compatible lifecycle with Claude Code as the provider."""

    name = "claude-code"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.claude_bin = str(os.getenv("CONVEYOR_CLAUDE_BIN", "claude")).strip() or "claude"
        self.claude_model = str(os.getenv("CONVEYOR_CLAUDE_MODEL", "")).strip()
        self.claude_permission_mode = str(os.getenv(
            "CONVEYOR_CLAUDE_PERMISSION_MODE", "acceptEdits"
        )).strip() or "acceptEdits"

    async def validate(self) -> None:
        await super().validate()
        binary = self.claude_bin
        if os.path.sep in binary:
            path = Path(binary).expanduser()
            if not path.is_file():
                raise RuntimeError(f"Claude Code CLI not found: {path}")
        elif shutil.which(binary) is None:
            raise RuntimeError(
                "Claude Code CLI is not installed or not on PATH. "
                "Install/authenticate `claude`, or set CONVEYOR_CLAUDE_BIN."
            )
        # Conveyor never enables Claude's bypass/auto modes.  FIX may opt down
        # to plan, but never up to a mode that can approve arbitrary tools.
        if self.claude_permission_mode not in {"acceptEdits", "plan"}:
            raise RuntimeError(
                "Invalid CONVEYOR_CLAUDE_PERMISSION_MODE; expected acceptEdits or plan"
            )

    def _claude_command(self, job: Job) -> list[str]:
        permission_mode = (
            "plan" if job.mode is JobMode.RUN else self.claude_permission_mode
        )
        tools = "Read,Glob,Grep" if job.mode is JobMode.RUN else "Read,Glob,Grep,Edit,Write"
        command = [
            self.claude_bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--safe-mode",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--tools", tools,
            "--permission-mode", permission_mode,
            "--name", f"conveyor-{job.id}",
        ]
        if self.claude_model:
            command.extend(["--model", self.claude_model])
        return command

    async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
        """Run one Claude Code attempt while preserving Conveyor lifecycle APIs."""
        command = self._claude_command(job)
        env = self._child_env()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=job.worktree_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=MAX_CLAUDE_STREAM_LINE,
        )
        job.process = process
        assert process.stdin is not None
        payload = (job.mode.stdin_prefix + self._prefetch_memory(job) + job.prompt).encode("utf-8")
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        assert process.stdout is not None
        assert process.stderr is not None
        assert job.log_path is not None
        assert job.final_message_path is not None

        final_text = ""
        stderr_chunks: list[str] = []
        stderr_bytes = 0

        async def read_stdout() -> None:
            nonlocal final_text
            logged_bytes = 0
            with job.log_path.open("ab") as log_file:
                while True:
                    try:
                        line = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        job.error = "Claude output event exceeded the safe size limit."
                        if process.returncode is None:
                            process.terminate()
                        return
                    if not line:
                        return
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    parsed = self._parse_claude_event(event)
                    if parsed.result:
                        final_text = truncate(redact_text(parsed.result), 16_000)
                    if parsed.error and not job.error:
                        job.error = truncate(redact_text(parsed.error), 3_000)
                    if parsed.progress:
                        job.last_event = truncate(redact_text(parsed.progress), 1200)
                        await on_progress(job.last_event)
                    if parsed.kind and job.external_id:
                        try:
                            emit_event(
                                self.settings,
                                parsed.kind,
                                str(job.external_id),
                                parsed.payload or {},
                                tool_call_id=parsed.tool_call_id,
                            )
                        except Exception:
                            # Event replay must never break provider execution.
                            pass
                    safe_record = self._safe_log_record(parsed)
                    if safe_record and logged_bytes < MAX_CLAUDE_LOG_BYTES:
                        encoded = (json.dumps(
                            safe_record, ensure_ascii=False, separators=(",", ":")
                        ) + "\n").encode("utf-8")
                        remaining = MAX_CLAUDE_LOG_BYTES - logged_bytes
                        if len(encoded) <= remaining:
                            log_file.write(encoded)
                            log_file.flush()
                            logged_bytes += len(encoded)
                        else:
                            logged_bytes = MAX_CLAUDE_LOG_BYTES

        async def read_stderr() -> None:
            nonlocal stderr_bytes
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                if stderr_bytes < MAX_CLAUDE_STDERR_BYTES:
                    decoded = line.decode("utf-8", errors="replace")
                    remaining = MAX_CLAUDE_STDERR_BYTES - stderr_bytes
                    chunk = decoded.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
                    stderr_chunks.append(chunk)
                    stderr_bytes += len(chunk.encode("utf-8"))

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        try:
            job.return_code = await asyncio.wait_for(
                process.wait(), timeout=self.settings.codex_timeout_seconds
            )
        except asyncio.TimeoutError:
            job.error = f"Timed out after {self.settings.codex_timeout_seconds} seconds."
            process.kill()
            job.return_code = await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        job.process = None

        if final_text:
            job.final_message_path.write_text(
                truncate(redact_text(final_text), 16_000), encoding="utf-8"
            )
        elif job.return_code == 0:
            job.final_message_path.write_text(
                truncate(redact_text(job.last_event or ""), 16_000), encoding="utf-8"
            )

        if stderr_chunks and job.return_code not in (None, 0) and not job.error:
            job.error = truncate(redact_text("".join(stderr_chunks)), 3000)
        if job.return_code is not None and job.return_code < 0 and job.cancel_requested:
            job.error = "cancelled"
        elif job.return_code is not None and job.return_code < 0 and not job.error:
            job.error = "Claude process terminated unexpectedly."

    @classmethod
    def _claude_event(cls, event: dict[str, Any]) -> tuple[str, str]:
        """Compatibility wrapper returning visible progress and final result."""
        parsed = cls._parse_claude_event(event)
        return parsed.progress, parsed.result

    @staticmethod
    def _parse_claude_event(event: dict[str, Any]) -> _ClaudeEvent:
        """Translate one Claude envelope without retaining inputs or reasoning."""
        event_type = str(event.get("type") or "")
        if event_type == "stream_event":
            inner = event.get("event") if isinstance(event.get("event"), dict) else {}
            delta = inner.get("delta") if isinstance(inner.get("delta"), dict) else {}
            delta_type = str(delta.get("type") or "")
            if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                text = truncate(redact_text(delta["text"]), 1200)
                return _ClaudeEvent(text, kind="assistant.delta", payload={"text": text})
            # thinking_delta and signature_delta are deliberately not surfaced.
            return _ClaudeEvent()

        if event_type == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")[:128]
                    tool_id = str(block.get("id") or "")[:128] or None
                    return _ClaudeEvent(
                        f"🔧 {name}...", kind="tool.started",
                        payload={"name": name, "status": "running"},
                        tool_call_id=tool_id,
                    )
            return _ClaudeEvent()

        if event_type == "user":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id") or "")[:128] or None
                    failed = bool(block.get("is_error"))
                    return _ClaudeEvent(
                        kind="tool.failed" if failed else "tool.completed",
                        payload={"status": "failed" if failed else "completed"},
                        tool_call_id=tool_id,
                    )
            return _ClaudeEvent()

        if event_type == "system" and event.get("subtype") == "api_retry":
            attempt = event.get("attempt")
            maximum = event.get("max_retries")
            progress = f"Claude retrying ({attempt}/{maximum})…"
            return _ClaudeEvent(progress, kind="system.status", payload={"text": progress})

        if event_type == "result":
            result = event.get("result")
            subtype = str(event.get("subtype") or "")
            is_error = bool(event.get("is_error")) or subtype not in {"", "success"}
            if isinstance(result, str):
                text = truncate(redact_text(result), 16_000)
                if is_error:
                    return _ClaudeEvent(error=text or subtype, kind="agent.error", payload={"text": text or subtype})
                return _ClaudeEvent(result=text, kind="assistant.completed", payload={"text": text})
            if is_error:
                error = truncate(redact_text(subtype or "Claude execution failed"), 3_000)
                return _ClaudeEvent(error=error, kind="agent.error", payload={"text": error})
            return _ClaudeEvent()

        if event_type == "error":
            message = event.get("message")
            error = truncate(redact_text(str(message or "Claude execution failed")), 3_000)
            return _ClaudeEvent(error=error, kind="agent.error", payload={"text": error})

        return _ClaudeEvent()

    @staticmethod
    def _safe_log_record(parsed: _ClaudeEvent) -> dict[str, Any] | None:
        """Persist only the already-sanitized public projection of an event."""
        if not parsed.kind:
            return None
        record: dict[str, Any] = {"kind": parsed.kind}
        if parsed.payload:
            record["payload"] = parsed.payload
        if parsed.tool_call_id:
            record["tool_call_id"] = parsed.tool_call_id
        return record
