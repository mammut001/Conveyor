"""Authenticated raw-audio POST /api/chat/voice adapter to the normal Web SSE chat."""
from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from channel.types import InboundMessage
from voice_transcription import (
    VoiceError, enabled, max_bytes, suffix_for, temporary_audio, transcribe_file,
)


def handle_voice_post(handler) -> None:
    if not enabled():
        handler._drain_body()
        handler._json(HTTPStatus.CONFLICT, {"error": "Voice messages are disabled (CONVEYOR_STT_ENABLED=1)"})
        return
    if "Transfer-Encoding" in handler.headers:
        handler.close_connection = True
        handler._json(HTTPStatus.BAD_REQUEST, {"error": "Chunked voice uploads are unsupported"})
        return
    try:
        size = int(handler.headers.get("Content-Length", ""))
        if not 0 < size <= max_bytes():
            raise VoiceError("Audio is empty or exceeds the STT upload size limit", 413)
        suffix = suffix_for(handler.headers.get("Content-Type", ""))
        query = parse_qs(urlparse(handler.path).query)
        session = (query.get("session_id") or [""])[0]
        from web_chat import WEB_CHAT_PREFIX, resolve_or_create_session
        session_info = resolve_or_create_session(
            handler.server.control, session, new_prefix=WEB_CHAT_PREFIX,
        )
        if not session_info or session_info[0] != "web":
            raise VoiceError("Invalid web session_id", 400)
        from handlers.chat import chat_enabled
        settings = handler.server.control.settings
        if not chat_enabled(settings):
            raise VoiceError("Chat tier is disabled", 409)
        with temporary_audio(suffix) as path:
            with path.open("wb") as out:
                remaining = size
                while remaining:
                    chunk = handler.rfile.read(min(65536, remaining))
                    if not chunk:
                        handler.close_connection = True
                        raise VoiceError("Incomplete audio upload", 400)
                    out.write(chunk)
                    remaining -= len(chunk)
            transcript = transcribe_file(path)
        channel, operator_id, source_chat_id, durable_id = session_info
        import uuid
        msg = InboundMessage(
            channel=channel, operator_id=operator_id, chat_id=source_chat_id,
            message_id=uuid.uuid4().hex, text=transcript, chat_type="p2p",
        )
        # Same server-side chat loop, session checks and approval mechanism.
        handler._handle_chat_stream(msg, settings, durable_id, transcript, heard=transcript)
    except VoiceError as exc:
        handler.close_connection = True  # unread invalid bodies must not be reused
        handler._json(exc.status, {"error": str(exc)})
    except (OSError, ValueError):
        handler.close_connection = True
        handler._json(HTTPStatus.BAD_GATEWAY, {"error": "Voice processing failed"})
