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

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def vendored_pi_bin() -> Path:
    """Path of the Pi binary installed by scripts/install_pi.sh (may not exist)."""
    return _REPO_ROOT / "vendor" / "pi" / "node_modules" / ".bin" / "pi"


def resolve_pi_bin(settings: Any) -> str:
    """CONVEYOR_PI_BIN > settings.pi_bin > vendored binary > `pi` on PATH."""
    explicit = str(os.getenv("CONVEYOR_PI_BIN") or getattr(settings, "pi_bin", "") or "").strip()
    if explicit:
        return explicit
    vendored = vendored_pi_bin()
    if vendored.is_file():
        return str(vendored)
    return "pi"


def sanitize_pi_provider_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", str(raw or "").strip().lower()).strip("-")
    return cleaned[:40] or "conveyor"


def build_pi_models_json(saved: dict[str, str]) -> dict[str, Any]:
    """models.json for Pi from the saved 模型设置 provider. Never contains the key."""
    api = "openai-responses" if saved.get("wire_api") == "responses" else "openai-completions"
    model = saved["model"]
    return {
        "providers": {
            sanitize_pi_provider_id(saved["provider_id"]): {
                "baseUrl": saved["base_url"],
                "api": api,
                "apiKey": "$" + saved["env_key"],
                "models": [{"id": model, "name": model}],
            }
        }
    }


@dataclass(frozen=True)
class _PiEvent:
    progress: str = ""
    result: str = ""
    error: str = ""
    kind: str = ""
    payload: dict[str, Any] | None = None
    tool_call_id: str | None = None


def normalize_pi_model(provider: str, raw_model: str) -> str:
    prov = (provider or "deepseek").strip().lower()
    model = (raw_model or "").strip()
    if prov == "deepseek":
        if model in {"", "deepseek-flash", "deepseek-chat", "deepseek-v3"}:
            return "deepseek-v4-flash"
        if model in {"deepseek-reasoner", "deepseek-r1", "deepseek-pro"}:
            return "deepseek-v4-pro"
    return model or "deepseek-v4-flash"


class PiBackend(CodexRunner):
    """CodexRunner-compatible lifecycle with Pi coding agent as the provider."""

    name = "pi"

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.pi_bin = resolve_pi_bin(settings)
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

    def _provider_credential_keys(self) -> set[str]:
        return {
            "DEEPSEEK_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "MINIMAX_API_KEY",
        }

    async def validate(self) -> None:
        await super().validate()
        binary = self.pi_bin
        if os.path.sep in binary:
            path = Path(binary).expanduser()
            if not path.is_file():
                raise RuntimeError(f"找不到 Pi 可执行文件：{path}。请到「执行者」页面安装内置 Pi，或检查 CONVEYOR_PI_BIN。")
        elif shutil.which(binary) is None:
            raise RuntimeError(
                "尚未安装 Pi。请到「执行者」页面点击安装内置 Pi，或设置 CONVEYOR_PI_BIN。"
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

    def _explicit_provider_config(self) -> bool:
        return any(
            str(value or "").strip()
            for value in (
                os.getenv("CONVEYOR_PI_PROVIDER"),
                os.getenv("CONVEYOR_PI_MODEL"),
                getattr(self.settings, "pi_provider", ""),
                getattr(self.settings, "pi_model", ""),
            )
        )

    def _saved_provider(self) -> dict[str, str] | None:
        """Saved 模型设置 provider to bridge into Pi, unless explicit config exists."""
        if self._explicit_provider_config():
            return None
        from provider_config import get_saved_provider
        saved = get_saved_provider(self.settings)
        if saved is None or not _ENV_KEY_RE.fullmatch(saved.get("env_key", "")):
            return None
        return saved

    def _pi_command(self, job: Job, provider: str | None = None, model: str | None = None) -> list[str]:
        tools = "read,bash,edit,write,grep,find,ls"
        return [
            self.pi_bin,
            "--mode", "json",
            "-p",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--provider", provider or self.pi_provider,
            "--model", model or self.pi_model,
            "--tools", tools,
            "--append-system-prompt", self._pi_system_prompt(job),
        ]

    async def _run_codex_attempt(self, job: Job, on_progress: ProgressCallback) -> None:
        """Run one Pi CLI attempt while preserving Conveyor lifecycle APIs."""
        from runner.operators.run import job_child_env
        env = job_child_env(self, job)
        saved = self._saved_provider()
        pi_dir = Path(self.settings.codex_task_root) / "pi-agent"
        dir_ready = False
        try:
            pi_dir.mkdir(parents=True, exist_ok=True)
            env["PI_CODING_AGENT_DIR"] = str(pi_dir)
            dir_ready = True
        except OSError:
            pass

        bridged = False
        if saved is not None and dir_ready:
            try:
                from provider_config import _atomic_write
                _atomic_write(
                    pi_dir / "models.json",
                    json.dumps(build_pi_models_json(saved), ensure_ascii=False, indent=2) + "\n",
                    0o600,
                )
                bridged = True
            except OSError:
                bridged = False

        if bridged and saved is not None:
            command = self._pi_command(
                job, sanitize_pi_provider_id(saved["provider_id"]), saved["model"]
            )
            # Key travels only through the child env under the saved env_key.
            env[saved["env_key"]] = saved["api_key"]
        else:
            command = self._pi_command(job)
            # Ensure DeepSeek API key is available to Pi.
            deepseek_key = (
                env.get("DEEPSEEK_API_KEY")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("CONVEYOR_CHAT_API_KEY")
                or getattr(self.settings, "chat_api_key", None)
                or ""
            ).strip()
            if deepseek_key:
                env["DEEPSEEK_API_KEY"] = deepseek_key
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
