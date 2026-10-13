# Voice transcription (opt in)

Voice messages are **off by default**. Set `CONVEYOR_STT_ENABLED=1` to enable
Telegram voice/audio messages and the optional microphone button in Web Chat.
DeepSeek chat credentials are **not** used for STT. Configure an independent
OpenAI-compatible speech-to-text provider supporting `POST /audio/transcriptions`:

```env
CONVEYOR_STT_ENABLED=1
CONVEYOR_STT_BASE_URL=https://api.openai.com/v1
CONVEYOR_STT_API_KEY=your-separate-provider-key
CONVEYOR_STT_MODEL=whisper-1
CONVEYOR_STT_MAX_BYTES=20971520
CONVEYOR_STT_MAX_DURATION_SECONDS=120
CONVEYOR_STT_TIMEOUT_SECONDS=45
```

Only HTTPS providers are accepted (HTTP is allowed for loopback test servers).
A URL ending in `/audio/transcriptions` is also accepted. Never expose the STT
key in browser code or the Web API. Multipart requests have a single attempt
without retries; provider failures are reported without returning its body.

**Telegram:** The same operator ID allowlist and group mention/reply gate apply
*before* fetching audio. Supported voice OGG/Opus and audio MP3/MP4/etc are
downloaded into a temporary directory. Server-provided Telegram duration and
size, plus downloaded file size, are checked before STT. The bot replies
`🎤 heard: …`, then passes the recognized words once through the existing
Telegram text dispatcher (commands, natural language and tool confirmations
behave as usual). Unauthorized senders are ignored.

**Web:** The chat composer microphone sends a raw MediaRecorder blob (WebM,
OGG or MP4) to authenticated `POST /api/chat/voice?session_id=…` using the
normal bearer token. The endpoint returns the **same SSE event stream** as
`POST /api/chat`, prefixed with `heard` to update the visible user message.
It uses the same Web Chat session resolution and approval flow. Request bodies
must have a Content-Length, cannot use chunked encoding, and are bounded
before allocation/reading. The server needs `ffprobe` (FFmpeg) installed to
measure Web recording duration. Undecodable / unmeasurable audio fails closed.
The browser automatically stops recording after 120 seconds (the server also
enforces its own configured maximum). Audio bytes are never retained.

Supported MIME types: `audio/webm`, `audio/ogg`, `audio/opus`,
`audio/mpeg`, `audio/mp4`, `audio/wav`, `audio/x-wav`, `audio/flac`.
The maximum upload size is 20 MiB, configurable downward; duration defaults
to 120 seconds, configurable up to 3600 seconds. Set both to the desired policy.

Transcripts are **untrusted text** subject to the same tool danger levels and
human approvals as typed text. Echoed transcripts enter regular chat history
but raw audio and STT responses are not stored. Audio temporary files are
deleted on success and error. Feishu audio is not implemented because the
existing Feishu SDK adapter does not expose a stable media download flow.
