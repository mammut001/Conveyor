"""runner/chat_client.py — direct chat-model calls (OpenAI-compatible).

The chat tier answers conversation without starting a Codex job. This is
an asynchronous client for ``POST {base_url}/chat/completions`` with SSE
streaming and tool completion. Total deadlines and cancellation close the
provider connection instead of leaving a background reader running.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from redaction import redact_text

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"

# Values accepted for the provider's reasoning-effort knob. Reasoning models
# (StepFun step-3.7-flash and friends) spend ``max_tokens`` on hidden reasoning
# tokens first; "low" keeps a simple question inside a small budget.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")

# Reasoning-exhaustion recovery is applied at most once per request.
_MAX_ATTEMPTS = 2
_EXPANDED_MIN_TOKENS = 4096
_EXPANDED_MAX_TOKENS = 16384

_REASONING_FIELDS = ("reasoning", "reasoning_content")


class ChatError(RuntimeError):
    """Chat call failed; callers fall back to the Codex agent."""


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60
    max_tokens: int = 1500
    temperature: float = 0.2
    # Optional provider hint ("none"|"minimal"|"low"|"medium"|"high"). Sent only
    # when set; unknown values are dropped so a typo cannot break a request.
    reasoning_effort: str | None = None


def clean_reasoning_effort(value: Any) -> str | None:
    """Normalize a reasoning-effort hint; anything unknown becomes None."""
    if not isinstance(value, str):
        return None
    effort = value.strip().lower()
    return effort if effort in REASONING_EFFORTS else None


def config_from_settings(settings: Any) -> ChatConfig | None:
    """ChatConfig when the chat tier is enabled and configured, else None."""
    if str(getattr(settings, "chat_mode", "off") or "off").lower() != "auto":
        return None
    api_key = getattr(settings, "chat_api_key", None)
    model = getattr(settings, "chat_model", None)
    if not api_key or not model:
        return None
    return ChatConfig(
        base_url=(getattr(settings, "chat_base_url", None) or DEFAULT_BASE_URL).rstrip("/"),
        api_key=api_key,
        model=model,
        timeout=int(getattr(settings, "chat_timeout_seconds", 60) or 60),
        max_tokens=int(getattr(settings, "chat_max_tokens", 1500) or 1500),
        reasoning_effort=clean_reasoning_effort(getattr(settings, "chat_reasoning_effort", None)),
    )


_ATTACHED_METADATA_KEYS = frozenset({"finish_reason", "usage"})


def _strip_attached_metadata(messages: list[dict]) -> list[dict]:
    cleaned = []
    for m in messages:
        if isinstance(m, dict) and any(k in m for k in _ATTACHED_METADATA_KEYS):
            cleaned.append({k: v for k, v in m.items() if k not in _ATTACHED_METADATA_KEYS})
        else:
            cleaned.append(m)
    return cleaned


def _payload(config: ChatConfig, messages: list[dict], stream: bool, tools=None) -> dict:
    payload: dict[str, Any] = {
        "model": config.model, "messages": _strip_attached_metadata(messages), "temperature": config.temperature,
        "max_tokens": config.max_tokens, "stream": stream,
    }
    effort = clean_reasoning_effort(config.reasoning_effort)
    if effort:
        payload["reasoning_effort"] = effort
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    return payload


def _headers(config: ChatConfig, stream: bool) -> dict[str, str]:
    return {
        "Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def _clean_chat_error(text: str, api_key: str | None) -> str:
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return redact_text(text)[:200]


def _provider_error(obj: Any) -> None:
    base = obj.get("base_resp") if isinstance(obj, dict) else None
    if isinstance(base, dict) and base.get("status_code"):
        raise ChatError(f"provider error: {base}")


def _choice0(obj: Any) -> dict | None:
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def _delta_text(obj: Any) -> str:
    """Visible answer text of a chunk — reasoning fields are never returned."""
    choice = _choice0(obj)
    delta = (choice.get("delta") or choice.get("message") or {}) if choice else {}
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


def _absorb_message(state: dict, message: Any, finish_reason: Any = None) -> None:
    """Record what one attempt produced: visible text, hidden reasoning, finish reason.

    ``message`` may be a completion ``message`` object or an SSE ``delta``;
    both carry ``content`` plus the optional ``reasoning`` fields.
    """
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content:
            state["content"] = (state.get("content") or "") + content
        elif content:
            state["content"] = state.get("content") or content
        for key in _REASONING_FIELDS:
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                state[key] = value
    if finish_reason is not None:
        state["finish_reason"] = finish_reason


def _reasoning_exhausted(message: Any, finish_reason: Any = None) -> bool:
    """True when the model burned the whole budget on hidden reasoning tokens.

    Reasoning models (e.g. StepFun step-3.7-flash) answer ``finish_reason:
    "length"`` with an empty ``content`` and a populated ``reasoning`` /
    ``reasoning_content`` field, so the operator would get a blank reply.
    ``message`` is a completion ``message`` or an accumulated stream state;
    ``finish_reason`` is checked when given.
    """
    if finish_reason is not None and finish_reason != "length":
        return False
    if not isinstance(message, dict):
        return False
    if message.get("tool_calls"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return False
    elif content:
        return False
    return any(
        isinstance(message.get(key), str) and message[key].strip() for key in _REASONING_FIELDS
    )


def _expanded_max_tokens(max_tokens: int) -> int:
    """Wider budget for the single reasoning-exhaustion retry."""
    return min(max(int(max_tokens) * 4, _EXPANDED_MIN_TOKENS), _EXPANDED_MAX_TOKENS)


async def _stream_attempt(
    config: ChatConfig, messages: list[dict], queue: asyncio.Queue, max_tokens: int, state: dict,
) -> bool | None:
    """Stream one attempt.

    Returns True when the attempt produced nothing but hidden reasoning and
    should be replayed with a wider budget, False when it is usable, and None
    when an error was already queued for the caller.
    """
    try:
        async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=True) as client:
            async with client.stream(
                "POST", f"{config.base_url}/chat/completions",
                json=_payload(dataclasses.replace(config, max_tokens=max_tokens), messages, True),
                headers=_headers(config, True),
            ) as response:
                response.raise_for_status()
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    obj = json.loads(await response.aread())
                    _provider_error(obj)
                    choice = _choice0(obj) or {}
                    _absorb_message(state, choice.get("message"), choice.get("finish_reason"))
                    await queue.put(("text", _delta_text(obj)))
                else:
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        _provider_error(obj)
                        choice = _choice0(obj) or {}
                        _absorb_message(state, choice.get("delta"), choice.get("finish_reason"))
                        text = _delta_text(obj)
                        if text:
                            await queue.put(("text", text))
    except httpx.TimeoutException:
        await queue.put(("error", "chat request timed out"))
    except httpx.HTTPStatusError as exc:
        await queue.put(("error", f"HTTP {exc.response.status_code}"))
    except Exception as exc:
        await queue.put(("error", _clean_chat_error(str(exc), config.api_key)))
    else:
        return _reasoning_exhausted(state, state.get("finish_reason"))
    return None


async def _stream_response(config: ChatConfig, messages: list[dict], queue: asyncio.Queue) -> None:
    """Read on the event loop; cancellation closes the response and client.

    A reasoning-only stream (hidden reasoning filled the budget, no visible
    text) is replayed once with a wider ``max_tokens``; reasoning text itself
    is never queued for the caller.
    """
    max_tokens = config.max_tokens
    for _ in range(_MAX_ATTEMPTS):
        state: dict[str, Any] = {}
        retry = await _stream_attempt(config, messages, queue, max_tokens, state)
        if retry is None:  # provider error already queued
            return
        if not retry:
            break
        expanded = _expanded_max_tokens(max_tokens)
        if expanded == max_tokens:  # nothing wider left to try
            break
        max_tokens = expanded
    await queue.put(("done", None))


async def stream_chat(config: ChatConfig, messages: list[dict]) -> AsyncIterator[str]:
    """Yield text with a total request deadline, including provider heartbeats."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    deadline = asyncio.get_running_loop().time() + config.timeout
    worker = asyncio.create_task(_stream_response(config, messages, queue))
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            kind, value = await asyncio.wait_for(queue.get(), remaining)
            if kind == "text":
                if value:
                    yield value
            elif kind == "error":
                raise ChatError(value)
            else:
                return
    except asyncio.TimeoutError as exc:
        raise ChatError("chat request timed out") from exc
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def _complete_response(
    config: ChatConfig, messages: list[dict], tools, max_tokens: int | None = None,
) -> dict:
    effective = (
        config if max_tokens is None or max_tokens == config.max_tokens
        else dataclasses.replace(config, max_tokens=max_tokens)
    )
    async with httpx.AsyncClient(timeout=effective.timeout, follow_redirects=True) as client:
        response = await client.post(
            f"{effective.base_url}/chat/completions", json=_payload(effective, messages, False, tools),
            headers=_headers(effective, False),
        )
        response.raise_for_status()
        try:
            obj = response.json()
        except ValueError as exc:
            raise ChatError("invalid JSON response") from exc
        _provider_error(obj)
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ChatError("invalid response: no choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ChatError("invalid response: message is not dict")
        out = dict(message)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and "finish_reason" not in out:
            out["finish_reason"] = finish_reason
        usage = obj.get("usage")
        if isinstance(usage, dict) and "usage" not in out:
            out["usage"] = usage
        return out


async def _complete_with_retry(config: ChatConfig, messages: list[dict], tools) -> dict:
    """One completion, replayed once when reasoning ate the whole budget."""
    out = await _complete_response(config, messages, tools)
    if not _reasoning_exhausted(out, out.get("finish_reason")):
        return out
    expanded = _expanded_max_tokens(config.max_tokens)
    if expanded == config.max_tokens:
        return out
    return await _complete_response(config, messages, tools, max_tokens=expanded)


async def complete_chat(config: ChatConfig, messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Return a tool-capable completion with cancellation and a total deadline."""
    try:
        return await asyncio.wait_for(_complete_with_retry(config, messages, tools), config.timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise ChatError("chat request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise ChatError(f"HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise ChatError(_clean_chat_error(str(exc), config.api_key)) from exc
