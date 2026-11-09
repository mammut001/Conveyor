"""Generic backend for third-party coding CLIs (agy, grok, cmd).

Reuses CodexRunner's job/worktree/diff/apply/discard lifecycle (like
ClaudeCodeBackend) and only replaces the provider attempt: the CLI runs in
the job worktree in non-interactive print mode, stdout lines are streamed as
(throttled) progress and the final stdout is the answer.  Conveyor stores no
credentials; the CLIs use their own login under HOME.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Callable

from redaction import redact_text, truncate
from runner.core import CodexRunner
from runner.types import Job, JobMode, ProgressCallback

logger = logging.getLogger("conveyor.runner.cli_backend")

MAX_STREAM_LINE = 1_000_000
MAX_LOG_BYTES = 2_000_000
MAX_FINAL_CHARS = 16_000
ERROR_TAIL_CHARS = 2_000
PROGRESS_INTERVAL_SECONDS = 2.0

# Env vars that must never reach these CLIs, whatever the isolation mode.
_BLOCKED_ENV = ("CONVEYOR_WEB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID")


@dataclass(frozen=True)
class CliConfig:
    worker_id: str
    binary: str
    default_model: str | None
    build_command: Callable[[str, str, str | None, Job], list[str]]
    # (binary, prompt, model, job) -> argv; branches on job.mode


class ModeNotAllowed(RuntimeError):
    pass


def _agy_command(binary: str, prompt: str, model: str | None, job: Job) -> list[str]:
    mode = "plan" if job.mode is JobMode.RUN else "accept-edits"
    return [binary, "-p", prompt, "--model", model or "gemini-3.8-flash-high",
            "--mode", mode, "--sandbox", "--output-format", "text"]


def _grok_command(binary: str, prompt: str, model: str | None, job: Job) -> list[str]:
    if job.mode is JobMode.RUN:
        # No verified read-only/plan option for grok: refuse rather than guess.
        raise ModeNotAllowed("Super Grok 暂不支持只读提问")
    command = [binary, "-p", prompt, "--always-approve",
               "--cwd", str(job.worktree_path), "--output-format", "plain"]
    if model:
        command.extend(["-m", model])
    return command


def _cmd_command(binary: str, prompt: str, model: str | None, job: Job) -> list[str]:
    model = model or "deepseek/deepseek-v4-flash"
    if job.mode is JobMode.RUN:
        return [binary, "-p", prompt, "--permission-mode", "plan", "--skip-onboarding",
                "-m", model, "--max-turns", "300"]
    return [binary, "-p", prompt, "--yolo", "-t", "--skip-onboarding",
            "-m", model, "--max-turns", "300"]


CLI_CONFIGS: dict[str, CliConfig] = {
    "agy": CliConfig("agy", "agy", "gemini-3.8-flash-high", _agy_command),
    "grok": CliConfig("grok", "grok", None, _grok_command),
    "cmd": CliConfig("cmd", "cmd", "deepseek/deepseek-v4-flash", _cmd_command),
}


class CliBackend(CodexRunner):
    """CodexRunner-compatible lifecycle with an external CLI as the provider."""

    def __init__(self, settings: Any, config: CliConfig) -> None:
        super().__init__(settings)
        self.config = config
        self.name = config.worker_id
        self.cli_bin = config.binary
        self.cli_model: str | None = None  # explicit override (tests)

    def _provider_credential_keys(self) -> set[str]:
        return set()

    def _chosen_model(self) -> str | None:
        if self.cli_model:
            return self.cli_model
        try:
            import workers
            state = workers.load_workers_state(self.settings)
            chosen = (state.get("models") or {}).get(self.config.worker_id)
            if chosen:
                return str(chosen)
        except Exception:
            logger.debug("could not read chosen model", exc_info=True)
        return self.config.default_model

    def _cli_command(self, job: Job, prompt: str, binary: str | None = None) -> list[str]:
        return self.config.build_command(
            binary or self.cli_bin, prompt, self._chosen_model(), job)

    def _cli_env(self, job: Job) -> dict[str, str]:
        from runner.operators.run import job_child_env
        env = job_child_env(self, job)
        for key in _BLOCKED_ENV:
            env.pop(key, None)
        return env

    async def validate(self) -> None:
        await super().validate()
        if os.path.sep in self.cli_bin:
            if not os.path.isfile(os.path.expanduser(self.cli_bin)):
                raise RuntimeError(f"{self.config.worker_id} CLI not found: {self.cli_bin}")
        elif shutil.which(self.cli_bin) is None:
            raise RuntimeError(
                f"{self.config.worker_id} CLI `{self.cli_bin}` is not installed or not on PATH."
            )

    async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
        from workers import mode_eligibility
        allowed, reason = mode_eligibility(self.settings, self.config.worker_id, job.mode)
        if not allowed:
            # Defence in depth: the pool already skips ineligible workers.
            job.error = reason or "mode not allowed"
            job.return_code = 1
            assert job.final_message_path is not None
            job.final_message_path.write_text("", encoding="utf-8")
            return
        env = self._cli_env(job)
        binary = shutil.which(self.cli_bin, path=env.get("PATH")) or self.cli_bin
        prompt = job.mode.stdin_prefix + self._prefetch_memory(job) + job.prompt
        command = self._cli_command(job, prompt, binary)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=job.worktree_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            limit=MAX_STREAM_LINE,
        )
        job.process = process
        assert process.stdout is not None
        assert job.log_path is not None
        assert job.final_message_path is not None

        lines: list[str] = []
        total_chars = 0

        async def read_output() -> None:
            nonlocal total_chars
            last_emit = 0.0
            logged = 0
            with job.log_path.open("ab") as log_file:
                while True:
                    try:
                        raw = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        job.error = "CLI output line exceeded the safe size limit."
                        if process.returncode is None:
                            process.terminate()
                        return
                    if not raw:
                        return
                    text = redact_text(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                    lines.append(text)
                    total_chars += len(text) + 1
                    # Keep memory bounded: only the tail matters.
                    while total_chars > 4 * MAX_FINAL_CHARS and len(lines) > 1:
                        total_chars -= len(lines.pop(0)) + 1
                    if logged < MAX_LOG_BYTES:
                        encoded = (text + "\n").encode("utf-8")
                        log_file.write(encoded[: MAX_LOG_BYTES - logged])
                        log_file.flush()
                        logged += len(encoded)
                    if text.strip():
                        now = time.monotonic()
                        if now - last_emit >= PROGRESS_INTERVAL_SECONDS:
                            last_emit = now
                            job.last_event = truncate(text.strip(), 1200)
                            await on_progress(job.last_event)

        reader = asyncio.create_task(read_output())
        try:
            job.return_code = await asyncio.wait_for(
                process.wait(), timeout=self.settings.codex_timeout_seconds)
        except asyncio.TimeoutError:
            job.error = f"Timed out after {self.settings.codex_timeout_seconds} seconds."
            process.kill()
            job.return_code = await process.wait()
        await asyncio.gather(reader, return_exceptions=True)
        job.process = None

        output = "\n".join(lines).strip()
        if job.return_code == 0:
            job.final_message_path.write_text(
                truncate(output, MAX_FINAL_CHARS) if output else "", encoding="utf-8")
        else:
            if job.return_code < 0 and job.cancel_requested:
                job.error = "cancelled"
            elif not job.error.startswith("Timed out"):
                tail = output[-ERROR_TAIL_CHARS:] if output else ""
                if tail:
                    job.error = tail
                elif job.return_code < 0:
                    job.error = f"{self.config.worker_id} process terminated unexpectedly."
                else:
                    job.error = f"{self.config.worker_id} exited with code {job.return_code}."
