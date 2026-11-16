"""worker_check.py — 执行者一键实测：「这个执行者现在真的能回答吗？」

「已就绪」只说明命令装好了，从没说明模型服务愿意回答。老板点一次「测试」，
这里就用和真实任务完全相同的 provider/env 拼法跑一次最小请求，把结果
（成功/失败 + 一句中文原因）按执行者存进 workers state，供执行者页面和
目标开始前的预检使用。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from conveyor.agents import workers
from conveyor.core.redaction import redact_text

# 只支持内置 Pi：别的执行者没有「模型设置」这一层，测不了。
SUPPORTED_WORKERS = ("pi",)
PROBE_PROMPT = "只回复两个字：好的"
DEFAULT_TIMEOUT_SECONDS = 45.0
MAX_DETAIL_CHARS = 600
# 测试没有工具要清理，给 pi 的 SIGTERM 宽限期比真实任务短。
CHECK_TERMINATE_GRACE_SECONDS = 2.0

KIND_OK = "ok"
KIND_AUTH = "auth"
KIND_QUOTA = "quota"
KIND_MODEL_MISSING = "model_missing"
KIND_RATE_LIMIT = "rate_limit"
KIND_TIMEOUT = "timeout"
KIND_NETWORK = "network"
KIND_CONFIG = "config"
KIND_OTHER = "other"

SETTINGS_HINT = "请到 设置 › 模型设置"

# 纯函数：把一段原始错误文本归成一类，方便单测，也方便复用。
_AUTH_ERROR_RE = re.compile(
    r"401|403|invalid[\s_-]*api[\s_-]*key|api[\s_-]*key[\s_-]*(?:is\s*)?(?:invalid|incorrect|wrong|missing)"
    r"|unauthorized|unauthenticated|authentication|auth(?:entication)?[\s_-]*(?:fail|error)"
    r"|鉴权|认证失败|密钥(?:错误|无效|不正确)",
    re.IGNORECASE,
)
_QUOTA_ERROR_RE = re.compile(
    r"402|insufficient[\s_-]*balance|insufficient[\s_-]*quota|credit[\s_-]*balance"
    r"|余额不足|额度|欠费|quota",
    re.IGNORECASE,
)
_RATE_LIMIT_ERROR_RE = re.compile(
    r"429|rate[\s_-]*limit|too[\s_-]*many[\s_-]*requests|限流",
    re.IGNORECASE,
)
_TIMEOUT_ERROR_RE = re.compile(r"timeout|timed[\s_-]*out|超时", re.IGNORECASE)
_NETWORK_ERROR_RE = re.compile(
    r"connection[\s_-]*(?:refused|reset|closed|error|failed)|econnrefused|econnreset|enotfound|eai_again"
    r"|getaddrinfo|name[\s_-]*(?:or[\s_-]*service[\s_-]*not[\s_-]*known|resolution)"
    r"|network[\s_-]*(?:is[\s_-]*)?(?:unreachable|down|error)|temporary[\s_-]*failure"
    r"|socket[\s_-]*hang[\s_-]*up|ssl|certificate|proxy|tunnel|dns",
    re.IGNORECASE,
)
_MODEL_ERROR_RE = re.compile(
    r"404|does\s*not\s*exist|doesn'?t\s*exist|not\s*found|no\s*permission|unknown\s*model|无权",
    re.IGNORECASE,
)


def classify_check_error(text: str) -> str:
    """一段原始错误文本 → 一个 kind。纯函数，不发请求、不读设置。"""
    raw = str(text or "")
    if not raw.strip():
        return KIND_OTHER
    low = raw.lower()
    if _AUTH_ERROR_RE.search(low):
        return KIND_AUTH
    if _QUOTA_ERROR_RE.search(low):
        return KIND_QUOTA
    if ("model" in low or "模型" in low) and _MODEL_ERROR_RE.search(low):
        return KIND_MODEL_MISSING
    if _RATE_LIMIT_ERROR_RE.search(low):
        return KIND_RATE_LIMIT
    if _TIMEOUT_ERROR_RE.search(low):
        return KIND_TIMEOUT
    if _NETWORK_ERROR_RE.search(low):
        return KIND_NETWORK
    return KIND_OTHER


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(text: str) -> str:
    """原始错误文本：先去掉密钥，再截到 600 字。"""
    return redact_text(str(text or "").strip())[:MAX_DETAIL_CHARS]


def _is_chat_bridge_provider(provider: str) -> bool:
    from runner import pi_agent

    return str(provider or "").strip() == pi_agent.PI_CHAT_BRIDGE_PROVIDER


def _resolved_provider_name(provider: str, model: str = "", provider_name: str = "") -> str:
    """Human-readable provider name for UI/messages; never expose internal bridge id."""
    from runner import pi_agent

    if _is_chat_bridge_provider(provider):
        return pi_agent.chat_bridge_provider_name(model)
    clean_name = str(provider_name or "").strip()
    return clean_name or str(provider or "").strip()


def _message(
    kind: str,
    *,
    provider: str = "",
    model: str = "",
    provider_name: str = "",
    latency_ms: int = 0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    detail: str = "",
) -> str:
    """给老板看的一句话。短、中文、能动手，绝不出现环境变量名。"""
    from runner import pi_agent

    is_bridge = _is_chat_bridge_provider(provider)
    display_provider = _resolved_provider_name(provider, model, provider_name)
    if kind == KIND_OK:
        if is_bridge:
            return f"可以用：{display_provider} 回复正常（{latency_ms} 毫秒）。"
        return f"可以用：{display_provider} / {model} 回复正常（{latency_ms} 毫秒）。"
    if kind == KIND_AUTH:
        auth_target = pi_agent.chat_bridge_provider_name("") if is_bridge else display_provider
        return f"模型服务拒绝了密钥（{auth_target}）。{SETTINGS_HINT} 重新填写 API Key。"
    if kind == KIND_QUOTA:
        quota_target = pi_agent.chat_bridge_provider_name("") if is_bridge else display_provider
        return f"模型服务额度用完了（{quota_target}）。请到服务商充值后再测试。"
    if kind == KIND_MODEL_MISSING:
        return f"模型 {model or '(未设置)'} 不存在或没有权限。{SETTINGS_HINT} 换一个模型。"
    if kind == KIND_RATE_LIMIT:
        return "模型服务暂时限流，稍后再试。"
    if kind == KIND_TIMEOUT:
        return f"{int(round(timeout))} 秒内没有回应。请检查网络或接口地址。"
    if kind == KIND_NETWORK:
        return "连不上模型服务。请检查接口地址和网络。"
    if kind == KIND_CONFIG:
        return detail or "本地配置还不完整，请到 执行者 页面按提示修好后再测试。"
    return "这次测试没有通过。请稍后再点一次「测试」，或到 执行者 页面重装执行者。"


def _result(
    *,
    ok: bool,
    kind: str,
    provider: str = "",
    model: str = "",
    provider_name: str = "",
    detail: str = "",
    latency_ms: int = 0,
    checked_at: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    safe_detail = _short(detail)
    resolved_name = _resolved_provider_name(provider, model, provider_name) if provider else ""
    return {
        "ok": ok,
        "worker_id": "pi",
        "provider": provider or "",
        "provider_name": resolved_name,
        "model": model or "",
        "latency_ms": int(latency_ms),
        "kind": kind,
        "message": _message(
            kind,
            provider=provider or "",
            model=model or "",
            provider_name=resolved_name,
            latency_ms=int(latency_ms),
            timeout=timeout,
            detail=safe_detail,
        ),
        "detail": safe_detail,
        "checked_at": checked_at or _utc_now(),
    }


def _unsupported_result(worker_id: str) -> dict[str, Any]:
    return {
        "ok": None,
        "worker_id": worker_id,
        "kind": "unsupported",
        "message": "这个执行者暂不支持一键测试",
        "checked_at": _utc_now(),
    }


def _probe_child_env(settings: Any) -> dict[str, str]:
    """和 PiBackend 子进程一致的环境，只是没有 Job 那一项。

    `runner/operators/run.job_child_env` 里不依赖 Job 的部分原样复用
    （隔离环境 / 过滤后的 os.environ、CODEX_TELEGRAM_JOB、RUNNER_HOME、
    以及出口代理），依赖 Job 的 CODEX_WORKSPACE_ROOT 跳过——测试不写任何
    项目文件。
    """
    from runner.operators import run as run_ops

    if getattr(settings, "env_isolation", False):
        from conveyor.core.subprocess_env import agent_env

        env = agent_env(
            settings,
            overrides={"CODEX_TELEGRAM_JOB": "1", "CODEX_RUNNER_HOME": str(run_ops.RUNNER_HOME)},
        )
    else:
        from security.secrets import child_env_from

        env = child_env_from(os.environ)
        env["CODEX_TELEGRAM_JOB"] = "1"
        env["CODEX_RUNNER_HOME"] = str(run_ops.RUNNER_HOME)
    try:
        from conveyor.browser.egress_proxy import proxy_env

        env = proxy_env(env, settings)
    except Exception:  # pragma: no cover - defensive: proxy is optional
        pass
    return env


def _pi_command(binary: str, selection: Any) -> list[str]:
    """最小只读探测命令。provider/model 与 `_pi_command` 同规则：要么都带，要么都不带。"""
    command = [
        binary,
        "--mode", "json",
        "-p",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
    ]
    if selection.model:
        command += ["--provider", selection.provider, "--model", selection.model]
    return command


async def _stop_process(process: asyncio.subprocess.Process) -> int | None:
    """超时后必须杀掉子进程，绝不留一个挂在服务器上。"""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            return await asyncio.wait_for(process.wait(), CHECK_TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
    return await process.wait()


async def _check_pi(settings: Any, timeout: float) -> dict[str, Any]:
    from runner import pi_agent

    started = time.monotonic()
    checked_at = _utc_now()
    binary = pi_agent.resolve_pi_bin(settings)
    if os.sep in binary and not Path(binary).expanduser().is_file():
        return _result(
            ok=False,
            kind=KIND_CONFIG,
            detail="没有找到 Pi 可执行文件，请到 执行者 页面点「安装内置 Pi」。",
            checked_at=checked_at,
            timeout=timeout,
        )

    env = _probe_child_env(settings)
    selection = pi_agent.resolve_pi_provider(settings, base_env=env)
    provider_name = ""
    if selection.chat_bridge or selection.provider == pi_agent.PI_CHAT_BRIDGE_PROVIDER:
        provider_name = pi_agent.chat_bridge_provider_name(selection.model)
    else:
        try:
            provider_name = str(pi_agent.describe_pi_provider(settings).get("provider_name") or "")
        except Exception:
            provider_name = ""
    if selection.config_error:
        return _result(
            ok=False,
            kind=KIND_CONFIG,
            provider=selection.provider,
            model=selection.model,
            provider_name=provider_name,
            detail=selection.config_error,
            latency_ms=_elapsed_ms(started),
            checked_at=checked_at,
            timeout=timeout,
        )
    try:
        pi_agent.prepare_pi_child_env(env, settings, selection)
    except Exception as exc:  # PiProviderConfigError / OSError: local config only
        return _result(
            ok=False,
            kind=KIND_CONFIG,
            provider=selection.provider,
            model=selection.model,
            provider_name=provider_name,
            detail=str(exc),
            latency_ms=_elapsed_ms(started),
            checked_at=checked_at,
            timeout=timeout,
        )

    command = _pi_command(binary, selection)
    workdir = Path(tempfile.mkdtemp(prefix="conveyor-worker-check-"))
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workdir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=pi_agent.MAX_PI_STREAM_LINE,
            )
        except Exception as exc:  # missing binary / permission: local config only
            return _result(
                ok=False,
                kind=KIND_CONFIG,
                provider=selection.provider,
                model=selection.model,
                provider_name=provider_name,
                detail=str(exc),
                latency_ms=_elapsed_ms(started),
                checked_at=checked_at,
                timeout=timeout,
            )

        assert process.stdin is not None
        try:
            process.stdin.write(PROBE_PROMPT.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        except OSError:
            pass  # pi already exited: keep reading whatever it printed

        final_text = ""
        agent_end_events = 0
        error_text = ""
        stderr_buffer = bytearray()

        async def read_stdout() -> None:
            nonlocal final_text, agent_end_events, error_text
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
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
                # 和 PiBackend 同一条解析规则，不另造一套更松的判断。
                parsed = pi_agent.PiBackend._parse_pi_event(event)
                if event.get("type") == "agent_end":
                    agent_end_events += 1
                    if parsed.result and not final_text:
                        final_text = parsed.result
                if parsed.error and not error_text:
                    error_text = parsed.error

        async def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(65_536)
                if not chunk:
                    return
                if len(stderr_buffer) < pi_agent.MAX_PI_STDERR_BYTES:
                    stderr_buffer.extend(chunk[: pi_agent.MAX_PI_STDERR_BYTES - len(stderr_buffer)])

        stdout_task = asyncio.ensure_future(read_stdout())
        stderr_task = asyncio.ensure_future(read_stderr())
        timed_out = False
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            returncode = await _stop_process(process)
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        latency_ms = _elapsed_ms(started)

        if timed_out:
            return _result(
                ok=False,
                kind=KIND_TIMEOUT,
                provider=selection.provider,
                model=selection.model,
                provider_name=provider_name,
                detail="",
                latency_ms=latency_ms,
                checked_at=checked_at,
                timeout=timeout,
            )
        # Same success rule as PiBackend._run_codex_attempt: exit 0, no error
        # event, exactly one agent_end, and a non-empty final assistant message.
        if returncode == 0 and not error_text and agent_end_events == 1 and final_text:
            return _result(
                ok=True,
                kind=KIND_OK,
                provider=selection.provider,
                model=selection.model,
                provider_name=provider_name,
                detail="",
                latency_ms=latency_ms,
                checked_at=checked_at,
                timeout=timeout,
            )

        raw_error = error_text or stderr_buffer.decode("utf-8", errors="replace").strip()
        if not raw_error:
            if returncode not in (None, 0):
                raw_error = f"Pi 退出了（返回码 {returncode}），没有给出失败原因。"
            else:
                raw_error = "Pi 没有返回完整的最终结果。"
        kind = classify_check_error(raw_error)
        return _result(
            ok=False,
            kind=kind,
            provider=selection.provider,
            model=selection.model,
            provider_name=provider_name,
            detail=raw_error,
            latency_ms=latency_ms,
            checked_at=checked_at,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def save_worker_check(settings: Any, result: dict[str, Any]) -> dict[str, Any]:
    """把最近一次测试结果存进 workers state 的 `last_check[worker_id]`。"""
    worker_id = str(result.get("worker_id") or "")
    if not worker_id:
        return result
    state = workers.load_workers_state(settings)
    last_check = state.setdefault("last_check", {})
    last_check[worker_id] = result
    try:
        workers.save_workers_state(settings, state)
    except Exception as exc:  # pragma: no cover - defensive
        workers.logger.warning("Failed saving worker check for %s: %s", worker_id, type(exc).__name__)
    return result


async def check_worker(
    settings: Any,
    worker_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """实测一个执行者现在能不能回答，并按执行者记住最近一次结果。

    不支持的执行者原样返回 `ok: None`，什么都不跑。
    """
    wid = str(worker_id or "").strip()
    if wid not in SUPPORTED_WORKERS:
        return _unsupported_result(wid)
    result = await _check_pi(settings, float(timeout))
    return save_worker_check(settings, result)
