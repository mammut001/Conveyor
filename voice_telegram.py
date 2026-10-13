"""Telegram voice/audio adapter; authenticated speech joins the existing dispatcher."""
from __future__ import annotations

from channel.auth import is_allowed
from channel.telegram import inbound_from_update
from handlers.context import is_addressed_to_bot
from voice_transcription import (
    VoiceError, enabled, max_bytes, max_seconds, temporary_audio, transcribe_async,
)


async def handle_voice(update, context, *, settings, dispatch_text) -> None:
    message = update.effective_message
    audio = (getattr(message, "voice", None) or getattr(message, "audio", None)) if message else None
    if audio is None:
        return
    inbound = inbound_from_update(update)
    # Preserve the same operator allowlist and group reply/mention gate as text.
    # Unauthorized voice is silent, including when feature is disabled.
    if not is_allowed(inbound, settings) or not is_addressed_to_bot(inbound):
        return
    if not enabled():
        await message.reply_text("🎤 Voice messages are disabled (CONVEYOR_STT_ENABLED=1).")
        return
    try:
        duration = getattr(audio, "duration", None)
        size = getattr(audio, "file_size", None)
        if not isinstance(duration, (int, float)) or not 0 < duration <= max_seconds():
            raise VoiceError("Audio exceeds duration limit or duration is unavailable", 413)
        if size is not None and (not isinstance(size, int) or not 0 < size <= max_bytes()):
            raise VoiceError("Audio exceeds the STT upload size limit", 413)
        suffix = ".ogg" if getattr(message, "voice", None) else ".mp3"
        if getattr(message, "audio", None):
            from voice_transcription import suffix_for
            suffix = suffix_for(getattr(audio, "mime_type", "") or "audio/mpeg")
        with temporary_audio(suffix) as path:
            remote = await context.bot.get_file(audio.file_id)
            if getattr(remote, "file_size", None) and remote.file_size > max_bytes():
                raise VoiceError("Audio exceeds the STT upload size limit", 413)
            await remote.download_to_drive(custom_path=path)
            transcript = await transcribe_async(path, duration=float(duration))
        await message.reply_text(f"🎤 heard: {transcript}")
        # Dispatch exactly once through the existing Telegram command/chat/approval path.
        await dispatch_text(update, transcript)
    except VoiceError as exc:
        await message.reply_text(f"🎤 {exc}")
    except Exception:
        # Do not log payload, URL or provider secrets.
        await message.reply_text("🎤 Could not process voice message. Please retry as text.")
