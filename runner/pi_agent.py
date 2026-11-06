"""Pi coding agent execution backend.

Reuses Conveyor's job, worktree, event stream, and apply/discard lifecycle
while executing tasks headlessly via the `pi` CLI (`@mariozechner/pi-coding-agent`)
with DeepSeek (or configured provider).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_events import emit_event
from redaction import redact_text, truncate
from runner.core import CodexRunner
from runner.types import Job, JobMode, ProgressCallback


MAX_PI_STREAM_LINE = 1_000_000
MAX_PI_LOG_BYTES = 2_000_000
MAX_PI_STDERR_BYTES = 32_000

DESTRUCTIVE_CONFIRM_TAG = "[OPERATOR_CONFIRMED_DESTRUCTIVE]"

# Env var NAME pi resolves from its own environment for each provider's API
# key (see pi's models.json ``apiKey`` entries, e.g. ``"apiKey":
# "STEPFUN_API_KEY"``). Only the mapped key of the selected provider is ever
# handed to the child process, so one provider's credential cannot leak into
# another provider's run.
PI_PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "stepfun": "STEPFUN_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}

# Operators may point Conveyor at a differently named key env var; anything
# that is not a valid env var name is ignored.
_PI_API_KEY_ENV_OVERRIDE = "CONVEYOR_PI_API_KEY_ENV"
_VALID_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def pi_api_key_env(provider: str) -> str:
    """Env var name carrying the API key of ``provider`` for the Pi CLI."""
    override = (os.getenv(_PI_API_KEY_ENV_OVERRIDE) or "").strip()
    if _VALID_ENV_NAME.fullmatch(override):
        return override
    prov = (provider or "").strip().lower()
    mapped = PI_PROVIDER_KEY_ENV.get(prov)
    if mapped:
        return mapped
    slug = re.sub(r"[^A-Z0-9]+", "_", (prov or "").upper()).strip("_")
    return f"{slug}_API_KEY"


@dataclass(frozen=True)
class _PiEvent:
    progress: str = ""
    result: str = ""
    error: str = ""
    kind: str = ""
    payload: dict[str, Any] | None = None
    tool_call_id: str | None = None


def normalize_pi_model(provider: str, raw_model: str) -> str:
    """Normalize the model name for the selected Pi provider.

    Only DeepSeek aliases are rewritten; every other provider's model name is
    passed through verbatim (possibly empty, in which case ``--model`` is
    omitted and pi picks its own default).
    """
    prov = (provider or "deepseek").strip().lower()
    model = (raw_model or "").strip()
    if prov == "deepseek":
        if model in {"", "deepseek-flash", "deepseek-chat", "deepseek-v3"}:
            return "deepseek-v4-flash"
        if model in {"deepseek-reasoner", "deepseek-r1", "deepseek-pro"}:
            return "deepseek-v4-pro"
    return model


class PiBackend(CodexRunner):
    """CodexRunner-compatible lifecycle with Pi coding agent as the provider."""

    name = "pi"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.pi_bin = str(
            os.getenv("CONVEYOR_PI_BIN")
            or getattr(settings, "pi_bin", "")
            or "pi"
        ).strip() or "pi"
        self.pi_provider = str(
            os.getenv("CONVEYOR_PI_PROVIDER")
            or getattr(settings, "pi_provider", "")
            or "deepseek"
        ).strip().lower() or "deepseek"
        raw_model = str(
            os.getenv("CONVEYOR_PI_MODEL")
            or getattr(settings, "pi_model", "")
            or getattr(settings, "chat_model", "")
            or "deepseek-v4-flash"
        ).strip()
        self.pi_model = normalize_pi_model(self.pi_provider, raw_model)
        self.pi_api_key_env = pi_api_key_env(self.pi_provider)

    def _provider_credential_keys(self) -> set[str]:
        return {
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "MINIMAX_API_KEY",
            self.pi_api_key_env,
        }

    def _console_secret(self, key: str) -> str:
        """Read one provider key from the Web Console secret store.

        Mirrors ``subprocess_env.build_child_env``'s use of the secrets file so
        an operator-stored key still reaches pi under credential isolation.
        Never logs or returns anything but the value; failures are silent.
        """
        try:
            from console_secrets import path_for, read_secret
            secrets_path = getattr(self.settings, "secrets_file", None)
            if secrets_path is None and getattr(self.settings, "codex_memory_root", None) is not None:
                secrets_path = path_for(self.settings)
            if secrets_path is None:
                return ""
            stored = read_secret(secrets_path, key)
        except Exception:
            return ""
        return stored.strip() if isinstance(stored, str) else ""

    async def validate(self) -> None:
        await super().validate()
        binary = self.pi_bin
        if os.path.sep in binary:
            path = Path(binary).expanduser()
            if not path.is_file():
                raise RuntimeError(f"Pi CLI not found: {path}")
        elif shutil.which(binary) is None:
            raise RuntimeError(
                "Pi CLI is not installed or not on PATH. "
                "Install `@mariozechner/pi-coding-agent` or set CONVEYOR_PI_BIN."
            )

    async def _create_worktree(self, job: Job) -> Path:
        if job.mode == JobMode.RUN and getattr(job, "reuse_worktree_path", None) is None:
            root = getattr(job, "workspace_root", None) or self.settings.codex_workspace_root
            resolved = Path(root).resolve()
            job.worktree_path = resolved
            job.worktree_created = False
            return resolved
        return await super()._create_worktree(job)

    def _pi_system_prompt(self, job: Job) -> str:
        confirmed = DESTRUCTIVE_CONFIRM_TAG in (job.prompt or "")
        if confirmed:
            return (
                "You are Pi, Conveyor's autonomous execution agent on the operator's Linux server. "
                "Reply concisely in the same language as the user (default Simplified Chinese). "
                "The operator has explicitly confirmed this destructive/deletion request "
                f"({DESTRUCTIVE_CONFIRM_TAG}). Execute the requested deletion carefully using your "
                "tools and report the exact files or resources removed."
            )
        return (
            "You are Pi, Conveyor's autonomous execution agent on the operator's Linux server. "
            "Reply concisely in the same language as the user (default Simplified Chinese). "
            "Execute the user's request directly using your bash, read, write, edit, grep, find, "
            "and ls tools without asking for confirmation on non-destructive tasks. "
            "When downloading files without an explicit target directory, save them under "
            "/srv/conveyor/downloads/ (create the directory if needed) and report the full file path. "
            "SAFETY POLICY: NEVER execute file or directory deletion (rm, rmdir, unlink, shred, "
            "git clean), package uninstallation, or database drops unless "
            f"{DESTRUCTIVE_CONFIRM_TAG} is present in the prompt. If a task requires deleting "
            "existing user files, stop and ask the user to confirm the deletion first."
        )

    def _pi_command(self, job: Job) -> list[str]:
        tools = "read,bash,edit,write,grep,find,ls"
        command = [
            self.pi_bin,
            "--mode", "json",
            "-p",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--provider", self.pi_provider,
        ]
        if self.pi_model:
            command += ["--model", self.pi_model]
        command += [
            "--tools", tools,
            "--append-system-prompt", self._pi_system_prompt(job),
        ]
        return command

    async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
        """Run one Pi CLI attempt while preserving Conveyor lifecycle APIs."""
        command = self._pi_command(job)
        from runner.operators.run import job_child_env
        env = job_child_env(self, job)

        # Ensure the API key of the selected provider is available to Pi.
        # Only DeepSeek keeps the legacy Conveyor fallbacks so a non-DeepSeek
        # provider never receives a credential it cannot use (and DeepSeek
        # behaviour is unchanged).
        key_name = self.pi_api_key_env
        value = str(env.get(key_name) or os.getenv(key_name) or "").strip()
        if not value and self.pi_provider == "deepseek":
            value = str(
                os.getenv("CONVEYOR_CHAT_API_KEY")
                or getattr(self.settings, "chat_api_key", None)
                or ""
            ).strip()
        if not value:
            value = self._console_secret(key_name)
        if value:
            env[key_name] = value

        pi_dir = Path(self.settings.codex_task_root) / "pi-agent"
        try:
            pi_dir.mkdir(parents=True, exist_ok=True)
            env["PI_CODING_AGENT_DIR"] = str(pi_dir)
        except OSError:
            pass
        env["PI_OFFLINE"] = "1"
        env["PI_TELEMETRY"] = "0"

        try:
            Path("/srv/conveyor/downloads").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=job.worktree_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=MAX_PI_STREAM_LINE,
        )
        job.process = process
        assert process.stdin is not None
        payload = (self._prefetch_memory(job) + job.prompt).encode("utf-8")
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
            streamed_text = ""
            last_delta_emit = 0.0
            # Pi emits one ``message_end`` per assistant turn, each with
            # per-request usage, so the totals have to be summed across the
            # whole job. Mirrors the Codex ``turn.completed`` accumulation in
            # ``runner/streaming.py:_capture_usage``.
            usage_totals: dict[str, int] = {}
            with job.log_path.open("ab") as log_file:
                while True:
                    try:
                        line = await process.stdout.readline()
                    except (ValueError, asyncio.LimitOverrunError):
                        job.error = "Pi output event exceeded the safe size limit."
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
                    parsed = self._parse_pi_event(event)
                    event_usage = self._usage_from_event(event)
                    if event_usage:
                        for key, value in event_usage.items():
                            usage_totals[key] = usage_totals.get(key, 0) + value
                        job.usage = dict(usage_totals)
                    if parsed.result:
                        final_text = truncate(redact_text(parsed.result), 16_000)
                    if parsed.error and not job.error:
                        job.error = truncate(redact_text(parsed.error), 3_000)
                    if parsed.kind == "tool.started":
                        streamed_text = ""
                    if parsed.kind == "assistant.delta" and parsed.progress:
                        streamed_text += parsed.progress
                        job.last_event = truncate(redact_text(streamed_text), 1200)
                        now_mono = time.monotonic()
                        if now_mono - last_delta_emit >= 0.12:
                            last_delta_emit = now_mono
                            await on_progress(f"{redact_text(streamed_text)[-3000:]} ▍")
                    elif parsed.progress:
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
                            pass
                    safe_record = self._safe_log_record(parsed)
                    if safe_record and logged_bytes < MAX_PI_LOG_BYTES:
                        encoded = (
                            json.dumps(safe_record, ensure_ascii=False, separators=(",", ":")) + "\n"
                        ).encode("utf-8")
                        remaining = MAX_PI_LOG_BYTES - logged_bytes
                        if len(encoded) <= remaining:
                            log_file.write(encoded)
                            log_file.flush()
                            logged_bytes += len(encoded)
                        else:
                            logged_bytes = MAX_PI_LOG_BYTES

        async def read_stderr() -> None:
            nonlocal stderr_bytes
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                if stderr_bytes < MAX_PI_STDERR_BYTES:
                    decoded = line.decode("utf-8", errors="replace")
                    remaining = MAX_PI_STDERR_BYTES - stderr_bytes
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
            job.error = "Pi process terminated unexpectedly."

    @staticmethod
    def _usage_from_event(event: dict[str, Any]) -> dict[str, int] | None:
        """Per-request token usage carried by one assistant ``message_end``.

        Pi (v0.73) emits one ``message_end`` per assistant turn, and each
        turn's usage describes a single request, so callers must SUM the
        returned values across every turn of a job.

        Keys are the Codex-compatible names consumed by
        ``scripts/metrics_report.py`` and ``scripts/health_snapshot.py``:
        Codex's ``input_tokens`` already includes cached tokens, so Pi's
        ``cacheRead`` is folded into ``input_tokens`` as well as reported
        separately as ``cached_input_tokens``.

        Returns None when the event is not an assistant ``message_end`` or
        carries no usage object. Non-int (and bool) values are ignored.
        """
        if str(event.get("type") or "") != "message_end":
            return None
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None

        def _int(key: str) -> int:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return 0
            return value

        cache_read = _int("cacheRead")
        return {
            "input_tokens": _int("input") + cache_read,
            "cached_input_tokens": cache_read,
            "output_tokens": _int("output"),
        }

    @classmethod
    def _pi_event(cls, event: dict[str, Any]) -> tuple[str, str]:
        parsed = cls._parse_pi_event(event)
        return parsed.progress, parsed.result

    @staticmethod
    def _extract_assistant_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()

    @classmethod
    def _parse_pi_event(cls, event: dict[str, Any]) -> _PiEvent:
        event_type = str(event.get("type") or "")

        if event_type == "tool_execution_start":
            name = str(event.get("toolName") or "tool")[:128]
            tool_id = str(event.get("toolCallId") or "")[:128] or None
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            detail = ""
            if name == "bash" and isinstance(args.get("command"), str):
                detail = " ".join(args["command"].strip().splitlines())[:64]
            elif name in {"read", "write", "edit", "ls"} and isinstance(args.get("path"), str):
                detail = args["path"].strip()[:64]
            elif name in {"grep", "find"} and isinstance(args.get("pattern"), str):
                detail = args["pattern"].strip()[:64]
            detail_clean = redact_text(detail).strip() if detail else ""
            label = f"🔧 {name}: {detail_clean}" if detail_clean else f"🔧 {name}..."
            return _PiEvent(
                progress=label,
                kind="tool.started",
                payload={"name": name, "status": "running"},
                tool_call_id=tool_id,
            )

        if event_type == "tool_execution_end":
            tool_id = str(event.get("toolCallId") or "")[:128] or None
            failed = bool(event.get("isError"))
            return _PiEvent(
                kind="tool.failed" if failed else "tool.completed",
                payload={"status": "failed" if failed else "completed"},
                tool_call_id=tool_id,
            )

        if event_type == "message_update":
            ame = (
                event.get("assistantMessageEvent")
                if isinstance(event.get("assistantMessageEvent"), dict)
                else {}
            )
            ame_type = str(ame.get("type") or "")
            if ame_type == "text_delta" and isinstance(ame.get("delta"), str) and ame["delta"]:
                text = redact_text(ame["delta"])[:1200]
                return _PiEvent(progress=text, kind="assistant.delta", payload={"text": text})
            return _PiEvent()

        if event_type == "message_end":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("role") == "assistant":
                stop_reason = str(message.get("stopReason") or "")
                err_msg = message.get("errorMessage")
                if stop_reason == "error" or err_msg:
                    error = truncate(redact_text(str(err_msg or "Pi execution failed")), 3_000)
                    return _PiEvent(error=error, kind="agent.error", payload={"text": error})
                text = cls._extract_assistant_text(message)
                if text:
                    clean = truncate(redact_text(text), 16_000)
                    return _PiEvent(result=clean)
            return _PiEvent()

        if event_type == "agent_end":
            messages = event.get("messages") if isinstance(event.get("messages"), list) else []
            for message in reversed(messages):
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                stop_reason = str(message.get("stopReason") or "")
                err_msg = message.get("errorMessage")
                if stop_reason == "error" or err_msg:
                    error = truncate(redact_text(str(err_msg or "Pi execution failed")), 3_000)
                    return _PiEvent(error=error, kind="agent.error", payload={"text": error})
                text = cls._extract_assistant_text(message)
                if text:
                    clean = truncate(redact_text(text), 16_000)
                    return _PiEvent(
                        result=clean,
                        kind="assistant.completed",
                        payload={"text": clean},
                    )
            return _PiEvent()

        if event_type == "error":
            message = event.get("message") or event.get("error")
            error = truncate(redact_text(str(message or "Pi execution failed")), 3_000)
            return _PiEvent(error=error, kind="agent.error", payload={"text": error})

        return _PiEvent()

    @staticmethod
    def _safe_log_record(parsed: _PiEvent) -> dict[str, Any] | None:
        if not parsed.kind:
            return None
        record: dict[str, Any] = {"kind": parsed.kind}
        if parsed.payload:
            record["payload"] = parsed.payload
        if parsed.tool_call_id:
            record["tool_call_id"] = parsed.tool_call_id
        return record
