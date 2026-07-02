# Desktop Screenshot Observe — P5.2

> **Status**: Implemented (local-first, read-only).
> **Not implemented**: Computer Use control, remote trigger queue, upload, OCR, LLM visual analysis.

P5.2 adds **read-only screenshot observe** on the operator's MacBook. Screenshots stay on disk by default. The VPS control plane receives metadata only when future remote wiring lands; in P5.2 it does not receive image bytes.

## Components

| Piece | Repo | Role |
|---|---|---|
| `capture-screen-helper` | `capture-your-screen` | macOS ScreenCaptureKit CLI; writes PNG + JSON metadata |
| `desktop_screenshot.py` | `Conveyor` | Validates helper output, stores metadata JSON |
| `desktop_agent.py --observe-once` | `Conveyor` | Local one-shot capture entry point |
| `desktop.screenshot.status` | `Conveyor` | Deterministic tool / NL route for observe status |

## Configuration

```env
CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
CONVEYOR_DESKTOP_SCREENSHOT_DIR=
CONVEYOR_DESKTOP_SCREENSHOT_MAX_BYTES=5000000
CONVEYOR_DESKTOP_SCREENSHOT_ALLOW_UPLOAD=false
```

- Helper path empty → observe disabled with a clear status message.
- Default screenshot dir: `CODEX_MEMORY_ROOT/desktop/screenshots`.
- Upload remains `false` in P5.2 even if set `true`.

## Local capture

```bash
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER=/usr/local/bin/capture-screen-helper
.venv/bin/python desktop_agent.py --observe-once
```

On success the agent prints safe JSON and writes:

- `.../desktop/screenshots/<screenshot_id>.png`
- `.../desktop/screenshots/<screenshot_id>.json`

Metadata fields: `screenshot_id`, `path`, `sha256`, `width`, `height`, `display_id`, `created_at`, `node_id`, `helper_version`.

No base64, OCR text, window titles, secrets, or prompt content are stored.

## Screen Recording permission

macOS requires **Screen Recording** permission for `capture-screen-helper`. Grant it under **System Settings → Privacy & Security → Screen Recording**.

Check without capturing:

```bash
capture-screen-helper --check-permission --json
```

## Natural language routing

Phrases such as `截图看看我电脑现在是什么`, `看一下 MacBook 屏幕`, and `take a screenshot on my desktop` route to `desktop.screenshot.status`. They do **not** start Codex and do **not** claim a screenshot was captured unless one actually was.

## Remote trigger

`POST /desktop/observe/request` returns `501 not implemented` in P5.2. Remote screenshot observe is documented for a future phase.

## Safety

- Read-only screenshot only
- No mouse, keyboard, clipboard, browser, or app automation
- No automatic upload
- No Gemini / LLM visual analysis
- No OCR
- No continuous capture stream
- Manual/local invocation first
- Explicit logs when observe runs (`conveyor.desktop_screenshot`)

**Computer Use control is still not implemented.**