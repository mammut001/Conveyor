"""Default-off OpenAI-compatible speech-to-text boundary (never executes tools).

No audio or provider response is logged or persisted; callers own short-lived
temporary files and route recognized text through the normal chat path.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_SECONDS = 120
_ALLOWED_SUFFIXES = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/opus": ".opus",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
}


class VoiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return os.getenv("CONVEYOR_STT_ENABLED", "").lower() in ("1", "true", "yes")


def _int_config(name: str, default: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise VoiceError(f"Invalid {name} configuration", 503) from None
    if not 1 <= value <= max_value:
        raise VoiceError(f"Invalid {name} configuration", 503)
    return value


def max_bytes() -> int:
    return _int_config("CONVEYOR_STT_MAX_BYTES", DEFAULT_MAX_BYTES, DEFAULT_MAX_BYTES)


def max_seconds() -> int:
    return _int_config("CONVEYOR_STT_MAX_DURATION_SECONDS", DEFAULT_MAX_SECONDS, 3600)


def suffix_for(mime: str) -> str:
    value = mime.split(";", 1)[0].strip().lower()
    if value not in _ALLOWED_SUFFIXES:
        raise VoiceError("Unsupported audio type (use WebM, OGG, MP3, MP4, WAV or FLAC)")
    return _ALLOWED_SUFFIXES[value]


@contextmanager
def temporary_audio(suffix: str):
    # TemporaryDirectory uses restrictive permissions and removes files even on errors.
    with tempfile.TemporaryDirectory(prefix="conveyor-stt-") as folder:
        yield Path(folder) / ("audio" + suffix)


def _probe_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        raise VoiceError("Audio duration validation requires ffprobe on the server", 503)
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        values = [data.get("format", {}).get("duration")]
        values += [item.get("duration") for item in data.get("streams", [])]
        candidates = [float(v) for v in values if v not in (None, "N/A")]
        candidates = [v for v in candidates if math.isfinite(v) and v >= 0]
        if candidates:
            return max(candidates)
        # MediaRecorder WebM often has no duration field. Estimate from the
        # last packet timestamp instead; still decoded by ffprobe, not client input.
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise ValueError("invalid audio")
        last = 0.0
        for line in result.stdout.splitlines():
            parts = line.split(",")
            try:
                start = float(parts[0])
                length = float(parts[1]) if len(parts) > 1 and parts[1] not in ("N/A", "") else 0
            except (TypeError, ValueError):
                continue
            if math.isfinite(start) and math.isfinite(length):
                last = max(last, start + max(0, length))
        if last > 0:
            return last
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        pass
    raise VoiceError("Could not determine audio duration", 400)


def validate_file(path: Path, duration: float | None = None) -> None:
    size = path.stat().st_size
    if not 0 < size <= max_bytes():
        raise VoiceError("Audio is empty or exceeds the STT upload size limit", 413)
    if duration is None:
        duration = _probe_duration(path)
    if not math.isfinite(duration) or duration <= 0 or duration > max_seconds():
        raise VoiceError("Audio exceeds duration limit or has invalid duration", 413)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _endpoint() -> str:
    base = os.getenv("CONVEYOR_STT_BASE_URL", "").strip().rstrip("/")
    parsed = urlsplit(base)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or
            parsed.fragment or (parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            ))):
        raise VoiceError("Set CONVEYOR_STT_BASE_URL to a valid HTTPS STT provider URL", 503)
    if not base.endswith("/audio/transcriptions"):
        base += "/audio/transcriptions"
    return base


def transcribe_file(path: Path, *, duration: float | None = None) -> str:
    if not enabled():
        raise VoiceError("Voice transcription is disabled (set CONVEYOR_STT_ENABLED=1)", 409)
    validate_file(path, duration)
    key = os.getenv("CONVEYOR_STT_API_KEY", "")
    model = os.getenv("CONVEYOR_STT_MODEL", "").strip()
    if not key or not model:
        raise VoiceError("Configure CONVEYOR_STT_API_KEY and CONVEYOR_STT_MODEL", 503)
    url = _endpoint()
    # Multipart without dependency, with an explicit one-request/no-retry policy.
    import uuid
    boundary = "conveyor" + uuid.uuid4().hex
    payload = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio{path.suffix}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = Request(
        url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    timeout = _int_config("CONVEYOR_STT_TIMEOUT_SECONDS", 45, 180)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise VoiceError("STT response exceeds size limit", 502)
    except HTTPError as exc:
        # Do not include remote body or headers (may contain credentials).
        raise VoiceError(f"Transcription provider returned HTTP {exc.code}", 502) from None
    except (URLError, TimeoutError, OSError):
        raise VoiceError("Transcription provider unavailable or timed out", 502) from None
    try:
        transcript = json.loads(raw)["text"]
        if not isinstance(transcript, str):
            raise ValueError()
        transcript = transcript.strip()
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise VoiceError("Invalid transcription response", 502) from None
    if not transcript:
        raise VoiceError("No speech was detected; try recording again", 422)
    if len(transcript) > 8000:
        raise VoiceError("Transcript exceeds the chat message limit", 413)
    return transcript


async def transcribe_async(path: Path, *, duration: float | None = None) -> str:
    return await asyncio.to_thread(transcribe_file, path, duration=duration)
