"""Default-off Feishu audio bridge to the shared STT and normal dispatcher.

FeishuChannel.download_resource uses its existing authenticated tenant client;
no token or media bytes are included in logs or diagnostics.
"""
from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

from channel.auth import is_allowed
from channel.types import InboundMessage
from handlers.context import is_addressed_to_bot
from voice_transcription import (
    VoiceError, enabled as stt_enabled, max_bytes, max_seconds,
    temporary_audio, transcribe_async,
)


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
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "libopus",
             "-b:a", "32k", str(destination)],
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
        # Feishu's message-resource API: type=file for audio. SDK supplies the
        # already configured tenant token, never copied into this module.
        data = await port.download_audio_resource(inbound, key)
        if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= max_bytes():
            raise VoiceError("Audio is unavailable or exceeds the STT upload size limit", 413)

        with temporary_audio(".opus") as source:
            source.write_bytes(data)
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
