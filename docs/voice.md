# Voice transcription (opt in)

Voice messages are **off by default**. Set `CONVEYOR_STT_ENABLED=1` to enable
Telegram voice/audio messages and the optional microphone button in Web Chat.
Feishu voice also requires `CONVEYOR_FEISHU_AUDIO=1` (off by default).
DeepSeek chat credentials are **not** used for STT. Configure an independent
OpenAI-compatible speech-to-text provider supporting `POST /audio/transcriptions`:

```env
CONVEYOR_STT_ENABLED=1
CONVEYOR_FEISHU_AUDIO=0
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
deleted on success and error.

**Feishu:** Opt in with both `CONVEYOR_STT_ENABLED=1` and
`CONVEYOR_FEISHU_AUDIO=1`. Only the allowlisted operator can send voice;
normal group mention and approval checks still apply. The SDK downloads the
`audio` message resource with `type=file` and its existing tenant token.
The Opus payload is converted to OGG using `ffmpeg`; `ffprobe` measures the
resulting duration before transcription. The same size and duration settings
apply. The bot replies `🎤 heard: …` before routing recognized words once
through its ordinary Feishu dispatcher. Server must have ffmpeg/ffprobe.
