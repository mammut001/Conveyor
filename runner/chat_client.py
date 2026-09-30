"""runner/chat_client.py — direct chat-model calls (OpenAI-compatible).

The chat tier answers conversation without starting a Codex job. This is
a tiny stdlib client for ``POST {base_url}/chat/completions`` with SSE
streaming or non-streaming tool completion, run in a worker thread so
the bot's event loop never blocks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, AsyncIterator

from redaction import redact_text

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"


class ChatError(RuntimeError):
    """Chat call failed; callers fall back to the Codex agent."""


@dataclass(frozen=True)
class ChatConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 60
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


def _request(
    config: ChatConfig,
    messages: list[dict],
    stream: bool,
    tools: list[dict] | None = None,
) -> urllib.request.Request:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )


def _delta_text(obj: dict) -> str:
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta") or choice.get("message") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


def _stream_worker(config: ChatConfig, messages: list[dict], emit) -> None:
    """Blocking SSE reader; calls ``emit(kind, value)``."""
    try:
        with urllib.request.urlopen(_request(config, messages, True), timeout=config.timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                # Endpoint ignored stream=true: one JSON body.
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
                emit("text", _delta_text(obj))
                return
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("base_resp", {}).get("status_code"):
                    raise ChatError(f"provider error: {obj.get('base_resp')}")
                text = _delta_text(obj) if isinstance(obj, dict) else ""
                if text:
                    emit("text", text)
    except urllib.error.HTTPError as exc:
        emit("error", f"HTTP {exc.code}")
    except Exception as exc:  # network, timeout, bad JSON, provider error
        emit("error", redact_text(str(exc))[:200])
    finally:
        emit("done", None)


async def stream_chat(config: ChatConfig, messages: list[dict]) -> AsyncIterator[str]:
    """Yield text chunks from the chat model; raise ChatError on failure."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(kind: str, value: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

    thread = threading.Thread(
        target=_stream_worker, args=(config, messages, emit), daemon=True,
    )
    thread.start()
    while True:
        kind, value = await queue.get()
        if kind == "text":
            yield value
        elif kind == "error":
            raise ChatError(value)
        elif kind == "done":
            return


def _clean_chat_error(text: str, api_key: str | None) -> str:
    msg = text
    if api_key:
        msg = msg.replace(api_key, "[REDACTED]")
    return redact_text(msg)[:200]


def _complete_worker(
    config: ChatConfig,
    messages: list[dict],
    tools: list[dict] | None,
) -> dict:
    try:
        req = _request(config, messages, stream=False, tools=tools)
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        try:
            obj = json.loads(data)
        except ValueError as exc:
            raise ChatError(f"invalid JSON response: {data[:100]}") from exc
        if isinstance(obj, dict) and obj.get("base_resp", {}).get("status_code"):
            raise ChatError(f"provider error: {obj.get('base_resp')}")
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise ChatError(f"invalid response: no choices in {data[:100]}")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ChatError("invalid response: message is not dict")
        return message
    except ChatError as exc:
        raise ChatError(_clean_chat_error(str(exc), config.api_key)) from exc
    except urllib.error.HTTPError as exc:
        raise ChatError(_clean_chat_error(f"HTTP {exc.code}", config.api_key)) from exc
    except Exception as exc:
        raise ChatError(_clean_chat_error(str(exc), config.api_key)) from exc


async def complete_chat(
    config: ChatConfig,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> dict:
    """Non-streaming chat completion; returns the first choice's message dict."""
    return await asyncio.to_thread(_complete_worker, config, messages, tools)

