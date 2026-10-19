"""runner/chat_client.py — direct chat-model calls (OpenAI-compatible).

The chat tier answers conversation without starting a Codex job. This is
an asynchronous client for ``POST {base_url}/chat/completions`` with SSE
streaming and tool completion. Total deadlines and cancellation close the
provider connection instead of leaving a background reader running.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from redaction import redact_text

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"


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


def _delta_text(obj: Any) -> str:
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or choices[0].get("message") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


async def _stream_response(config: ChatConfig, messages: list[dict], queue: asyncio.Queue) -> None:
    """Read on the event loop; cancellation closes the response and client."""
    try:
        async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=True) as client:
            async with client.stream(
                "POST", f"{config.base_url}/chat/completions",
                json=_payload(config, messages, True), headers=_headers(config, True),
            ) as response:
                response.raise_for_status()
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    obj = json.loads(await response.aread())
                    _provider_error(obj)
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


async def _complete_response(config: ChatConfig, messages: list[dict], tools) -> dict:
    async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=True) as client:
        response = await client.post(
            f"{config.base_url}/chat/completions", json=_payload(config, messages, False, tools),
            headers=_headers(config, False),
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


async def complete_chat(config: ChatConfig, messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Return a tool-capable completion with cancellation and a total deadline."""
    try:
        return await asyncio.wait_for(_complete_response(config, messages, tools), config.timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        raise ChatError("chat request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise ChatError(f"HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise ChatError(_clean_chat_error(str(exc), config.api_key)) from exc
