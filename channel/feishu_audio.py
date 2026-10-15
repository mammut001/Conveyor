"""Default-off Feishu audio bridge to the shared STT and normal dispatcher.

Feishu audio uses bounded response reads and the SDK tenant-token cache;
no token or media bytes are included in logs or diagnostics.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from channel.feishu_ffmpeg_policy import opus_to_ogg_command
from channel.auth import is_allowed
from channel.types import InboundMessage
from handlers.context import is_addressed_to_bot
from voice_transcription import (
    VoiceError, enabled as stt_enabled, max_bytes, max_seconds,
    temporary_audio, transcribe_async,
)



_RESOURCE_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _tenant_token(channel: Any) -> str:
    # The same SDK tenant token manager/cache as normal Feishu API calls.
    from lark_oapi.core.token import TokenManager
    return TokenManager.get_self_tenant_token(channel._client.config)


def _open_resource(request: Request, *, timeout: float):
    # Forbid redirects and inherited proxies carrying bearer credentials.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def _download_to_path(
    channel: Any, message_id: str, file_key: str, dest: Path, limit: int,
) -> None:
    """Stream directly to a private file. Read at most limit+1 body bytes."""
    if (
        not isinstance(file_key, str) or not _RESOURCE_KEY.fullmatch(file_key)
        or not isinstance(message_id, str) or not _RESOURCE_KEY.fullmatch(message_id)
    ):
        raise VoiceError("Invalid Feishu resource identifier", 400)
    config = channel._client.config
    domain = config.domain.rstrip("/")
    parsed = urlsplit(domain)
    if (
        parsed.username or parsed.password or parsed.path or parsed.query
        or parsed.fragment or not parsed.hostname
        or (parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        ))
    ):
        raise VoiceError("Invalid Feishu API origin", 503)
    url = (
        domain + "/open-apis/im/v1/messages/" + quote(message_id, safe="")
        + "/resources/" + quote(file_key, safe="") + "?" + urlencode({"type": "file"})
    )
    try:
        request = Request(
            url, method="GET",
            headers={
                "Authorization": "Bearer " + _tenant_token(channel),
                "Accept-Encoding": "identity",
            },
        )
        with _open_resource(request, timeout=min(float(config.timeout or 30), 30)) as response:
            if getattr(response, "status", 200) != 200:
                raise VoiceError("Feishu audio download failed", 502)
            headers = response.headers
            content_length = headers.get("Content-Length")
            if content_length is not None:
                if not content_length.isascii() or not content_length.isdecimal():
                    raise VoiceError("Invalid Feishu audio Content-Length", 502)
                if int(content_length) > limit:
                    raise VoiceError("Audio exceeds the STT upload size limit", 413)
            if (headers.get("Content-Encoding") or "identity").lower() != "identity":
                raise VoiceError("Unsupported Feishu audio encoding", 502)
            remaining = limit
            with dest.open("wb") as output:
                while True:
                    # One additional byte detects a missing/lying Content-Length.
                    chunk = response.read(min(65536, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise VoiceError("Audio exceeds the STT upload size limit", 413)
                    output.write(chunk)
            if remaining == limit:
                raise VoiceError("Audio is empty", 413)
    except VoiceError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, ValueError, TypeError):
        # Never include upstream exception text (token, URL, response, or body).
        raise VoiceError("Feishu audio download failed", 502) from None


async def download_bounded_resource(
    channel: Any, message_id: str, file_key: str, dest: Path, limit: int,
) -> None:
    await asyncio.to_thread(_download_to_path, channel, message_id, file_key, dest, limit)


def audio_enabled() -> bool:
    return (
        stt_enabled()
        and os.getenv("CONVEYOR_FEISHU_AUDIO", "").lower() in ("1", "true", "yes")
    )


def is_audio_message(event: Any) -> bool:
    """The lark Channel normalizer calls Feishu msg_type raw_content_type."""
    return (
        getattr(event, "raw_content_type", None) == "audio"
        or getattr(getattr(event, "content", None), "kind", None) == "audio"
    )


def _audio_metadata(event: Any) -> tuple[str, float | None, int | None]:
    content = getattr(event, "content", None)
    resources = [
        r for r in (getattr(event, "resources", None) or ())
        if getattr(r, "type", None) == "audio"
    ]
    resource = resources[0] if resources else None
    key = getattr(content, "file_key", None) or getattr(resource, "file_key", None)
    if not isinstance(key, str) or not key:
        raise VoiceError("Missing Feishu audio file key", 400)

    duration_ms = getattr(content, "duration_ms", None)
    if duration_ms is None:
        duration_ms = getattr(resource, "duration_ms", None)
    duration = None
    if duration_ms is not None:
        try:
            duration = float(duration_ms) / 1000.0
        except (TypeError, ValueError, OverflowError):
            raise VoiceError("Invalid audio duration", 413) from None
        if not math.isfinite(duration) or not 0 < duration <= max_seconds():
            raise VoiceError("Audio exceeds duration limit or has invalid duration", 413)

    # When the upstream includes size metadata, reject before any download.
    raw = getattr(content, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    size = raw.get("file_size")
    if size is not None:
        try:
            size = int(size)
        except (ValueError, TypeError, OverflowError):
            raise VoiceError("Invalid audio size", 413) from None
        if not 0 < size <= max_bytes():
            raise VoiceError("Audio exceeds the STT upload size limit", 413)
    return key, duration, size


def _convert_opus(source: Path, destination: Path) -> None:
    """Repackage/encode Feishu Opus into OGG accepted by the STT boundary."""
    if not shutil.which("ffmpeg"):
        raise VoiceError("Feishu audio requires ffmpeg on the server", 503)
    try:
        completed = subprocess.run(
            opus_to_ogg_command(source, destination),
            capture_output=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceError("Could not convert Feishu Opus audio", 422) from None
    if completed.returncode != 0 or not destination.is_file():
        # Never surface FFmpeg stderr: it can contain attacker-controlled data.
        raise VoiceError("Could not convert Feishu Opus audio", 422)


async def handle_audio(
    event: Any,
    inbound: InboundMessage,
    *,
    port: Any,
    settings: Any,
    dispatch_text: Callable[[str], Awaitable[None]],
) -> bool:
    """Return True for an opted-in audio event (even if rejected).

    The caller's usual group-mention, operator and approval policies remain
    authoritative; we repeat the first two gates so direct use stays safe.
    """
    if not is_audio_message(event) or not audio_enabled():
        return False
    if (
        not getattr(settings, "lark_allowed_open_id", None)
        or not is_allowed(inbound, settings)
        or not is_addressed_to_bot(inbound)
    ):
        return True  # silent, with no API request or transcript preview

    try:
        key, _, _ = _audio_metadata(event)
        if not inbound.message_id:
            raise VoiceError("Missing Feishu message ID", 400)
        with temporary_audio(".opus") as source:
            # Enforce the limit as bytes arrive, not after an eager SDK read.
            await port.download_audio_resource(inbound, key, source, max_bytes())
            converted = source.with_suffix(".ogg")
            await asyncio.to_thread(_convert_opus, source, converted)
            if not 0 < converted.stat().st_size <= max_bytes():
                raise VoiceError("Converted audio exceeds the STT upload size limit", 413)
            # The shared STT boundary probes the *decoded* duration with
            # ffprobe, not merely the untrusted Feishu metadata.
            transcript = await transcribe_async(converted)
        await port.reply(inbound, f"🎤 heard: {transcript}")
        # Same Feishu dispatch/approval path as ordinary typed text.
        await dispatch_text(transcript)
    except VoiceError as exc:
        await port.reply(inbound, f"🎤 {exc}")
    except Exception:
        # No exception repr/traceback: never log audio, keys or tenant token.
        await port.reply(inbound, "🎤 Could not process voice message. Please retry as text.")
    return True
