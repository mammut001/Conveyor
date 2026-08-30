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
from pathlib import Path
from typing import Any

from redaction import redact_text, truncate
from runner.core import CodexRunner
from runner.types import Job, ProgressCallback


class ClaudeCodeBackend(CodexRunner):
    """CodexRunner-compatible lifecycle with Claude Code as the provider."""

    name = "claude-code"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.claude_bin = str(os.getenv("CONVEYOR_CLAUDE_BIN", "claude")).strip() or "claude"
        self.claude_model = str(os.getenv("CONVEYOR_CLAUDE_MODEL", "")).strip()
        self.claude_permission_mode = str(
            os.getenv("CONVEYOR_CLAUDE_PERMISSION_MODE", "acceptEdits")
        ).strip() or "acceptEdits"

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
        if self.claude_permission_mode not in {
            "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"
        }:
            raise RuntimeError("Invalid CONVEYOR_CLAUDE_PERMISSION_MODE")

    def _claude_command(self, job: Job) -> list[str]:
        command = [
            self.claude_bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", self.claude_permission_mode,
            "--name", f"conveyor-{job.id}",
        ]
        if self.claude_model:
            command.extend(["--model", self.claude_model])
        command.append(job.mode.stdin_prefix + self._prefetch_memory(job) + job.prompt)
        return command

    async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
        """Run one Claude Code attempt while preserving Conveyor lifecycle APIs."""
        command = self._claude_command(job)
        env = os.environ.copy()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=job.worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        job.process = process
        assert process.stdout is not None
        assert process.stderr is not None
        assert job.log_path is not None
        assert job.final_message_path is not None

        final_text = ""
        stderr_chunks: list[str] = []

        async def read_stdout() -> None:
            nonlocal final_text
            with job.log_path.open("ab") as log_file:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        return
                    log_file.write(line)
                    log_file.flush()
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    progress, result = self._claude_event(event)
                    if result:
                        final_text = result
                    if progress:
                        job.last_event = truncate(redact_text(progress), 1200)
                        await on_progress(job.last_event)

        async def read_stderr() -> None:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                stderr_chunks.append(line.decode("utf-8", errors="replace"))

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
        if job.return_code is not None and job.return_code < 0:
            job.error = "cancelled"

    @staticmethod
    def _claude_event(event: dict[str, Any]) -> tuple[str, str]:
        """Return (user-visible progress, final result), excluding reasoning."""
        event_type = str(event.get("type") or "")
        if event_type == "stream_event":
            inner = event.get("event") if isinstance(event.get("event"), dict) else {}
            delta = inner.get("delta") if isinstance(inner.get("delta"), dict) else {}
            delta_type = str(delta.get("type") or "")
            if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                return delta["text"], ""
            # thinking_delta and signature_delta are deliberately not surfaced.
            return "", ""

        if event_type == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")[:128]
                    return f"🔧 {name}...", ""
            return "", ""

        if event_type == "system" and event.get("subtype") == "api_retry":
            attempt = event.get("attempt")
            maximum = event.get("max_retries")
            return f"Claude retrying ({attempt}/{maximum})…", ""

        if event_type == "result":
            result = event.get("result")
            if isinstance(result, str):
                return "", result
            return "", ""

        return "", ""
