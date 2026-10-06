# Changelog

All notable changes to Conveyor will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Live screen shows the host pointer** while you are only watching, so you can see where the Agent is pointing (captures carry no cursor image); a pointer move alone now produces a new frame.
- **Deploy reports systemd unit drift**: `deploy_vps.sh` warns when an installed `conveyor-*` unit differs from the repo copy, since deploys cannot install unit files.
- **Web Console on a private VPN address** (`CONVEYOR_WEB_EXTRA_HOSTS`): the console can also listen on the host's WireGuard/Tailscale address so a phone on the VPN opens it directly, without an SSH tunnel. Same server object and bearer token as loopback; wildcard and public addresses are refused because the extra listeners are plain HTTP. See `docs/mobile_web.md`.
- **Remember the console token on this device** (opt-in checkbox on the unlock screen): keeps the token in `localStorage` so a new tab does not ask again; the new **Lock** button, or a rejected token, forgets it. Unchecked, the token still lives in the tab only.
- **Live screen: click the screen to take control**, and the view refreshes at 10 fps for a moment after each input (and immediately after it) instead of waiting for the next idle frame.
- **Live screen** (`CONVEYOR_LIVE_SCREEN_ENABLED`, default off): the Web Console's Computer section shows a live thumbnail of the host desktop; clicking it opens the screen full size, and **Take control** forwards mouse, keyboard and pasted text while holding the same exclusive lease as Secure Human Takeover, so the Agent pauses until release. No VNC port, second password or sidecar: frames (`/api/screen/frame`) and input (`/api/screen/input`) go through the console's authenticated API, are never stored or logged, and an abandoned viewer loses control within 45 seconds. See `docs/live_screen.md`.
- **Configurable login password unblocking for Computer Use** (`CONVEYOR_COMPUTER_ALLOW_LOGIN_PASSWORDS`, default off): Operators can allow login password entry during hands-free computer use by enabling `CONVEYOR_COMPUTER_ALLOW_LOGIN_PASSWORDS=true` or removing `password`/`passcode` from `CONVEYOR_COMPUTER_BLOCKED_KEYWORDS`. Login credentials (`password`, `passcode`, `密码`, `口令`) are decoupled from unbypassable hard-blocked words. Financial, payment, transfer, and destructive contexts (`bank`, `payment`, `crypto`, `keychain`, `system settings`, `delete account`, `支付`, `转账`, `付款`, `交易密码`, `收银台`, `银行`) remain strictly hard-blocked and cannot be bypassed.
- **A chat window stays open on the VPS desktop**: `desktop_chat_window.py` posts to the local web console, so a sentence typed there takes the same computer-use route as the phone. `deploy/conveyor-desktop-chat.service` starts it inside the XFCE session.
- **Parallel read-only subagents for the chat tier** (`CONVEYOR_SUBAGENTS_ENABLED`, default off, roadmap P3-1 "并行执行 / 后台子 Agent", matrix row 5 "并行子 Agent"): Allows the chat agent to fan out research questions into parallel, read-only subagents using the `agents.parallel` tool. Each task executes in an independent conversation tool loop with restricted read-only tools and no conversation history, long-term memory, or skills preload. Strictly read-only policy: subagents cannot call write or destructive tools, with attempts immediately refused without confirmation or execution. Depth guard prevents recursive subagent spawning. Process-wide concurrency controlled via `asyncio.Semaphore` (`CONVEYOR_SUBAGENTS_MAX_PARALLEL`, default 3) with per-subagent timeout (`CONVEYOR_SUBAGENTS_TIMEOUT_SECONDS`, default 90), tool step cap (`CONVEYOR_SUBAGENTS_MAX_STEPS`, default 3), task count cap (`CONVEYOR_SUBAGENTS_MAX_TASKS`, default 4), and character output limit (`CONVEYOR_SUBAGENTS_MAX_OUTPUT_CHARS`, default 2500). Outputs are sanitized, redacted, and wrapped in `<tool-result untrusted="true">`. Live progress reported via bot progress edits and Web Console SSE `subagent` events, rendered in Web Chat as an expandable progress card group and collapsed summary badge. Audit logging with `action="subagent"` and redacted previews. Feature status reported in `system_status()["features"]["subagents"]`. See `docs/subagents.md`.
- **Chat tools: search-backed tools need a backend**: `web.search`, `research.run` and `research.project` are no longer offered to the chat tier (or subagents) when `WEB_SEARCH_BACKEND` is `disabled`, even if allowlisted; the unconfigured-search message now names `WEB_SEARCH_BACKEND` and the supported backends.
- **Cross-channel approval relay** (`CONVEYOR_APPROVAL_RELAY_ENABLED`, default off, roadmap P2-2 "Telegram/飞书/Web 三端同步"): Shared SQLite store (`approval_relay.db`, WAL mode, file mode 0600) enabling operators to approve dangerous tool actions from wherever they are across Web Console, Telegram bot, and Feishu bot. When an action is published on any channel, outbound notifications fan out to all other enabled channels (`CONVEYOR_APPROVAL_RELAY_CHANNELS`) with tool name, summary, danger level, and redacted previews (secrets screened via `redact_text`). Atomic compare-and-set decision (`approval_relay.decide`) enforces that the first decision wins everywhere; all surfaces' notification messages are automatically updated to reflect the final outcome and clear buttons. The origin process claims decisions via an asynchronous polling consumer (`RelayConsumer`) and executes confirmed actions locally, prefixing outputs with the deciding surface. Web Approval Inbox (`GET /api/approval-inbox`) aggregates foreign relay items with source badges and execution notices; decisions via Web endpoints return immediately with acknowledgement while execution proceeds in the owning bot process. Audit events recorded with `action="relay_decided"`. See `docs/approval_relay.md`.
- **MCP connector support for the chat tier** (`CONVEYOR_MCP_ENABLED`, default off, roadmap P2-1 / row 11): Connects the chat tier (Web chat, Telegram, Feishu, routines) to operator-configured Model Context Protocol (MCP) servers over **stdio** (subprocesses) and **streamable HTTP** (SSE / JSON-RPC 2.0). Implemented using standard library only (no external MCP package dependencies). Servers are configured in JSON (default `<codex_memory_root>/mcp_servers.json`) with strict validation (server names `^[a-z0-9][a-z0-9_-]{0,31}$`, absolute commands, valid URLs, required `allow_tools`, `read_only_tools` without wildcards, timeouts 1–120s, output caps 200–20,000 chars, max 20 servers). Enforces strict child process isolation (stdio subprocesses get only `PATH`, `LANG`/`LC_ALL`, private `HOME` `<codex_memory_root>/mcp_home/<name>` with mode 0700, literal `env`, and `env_from`/`headers_from` restricted strictly to process variables prefixed with `MCP_`; host secrets never forwarded). Exposes tools as OpenAI functions (`mcp.<server>.<tool>`), preserves real object `inputSchema`, runs `read_only_tools` automatically wrapped in `<tool-result untrusted="true">`, and routes all other tools to operator confirmation via `_request_confirmation` and the Unified Approval Inbox with canonical JSON arguments. Re-validates policies at execution time. Web Console management endpoints (`GET /api/mcp/servers`, `POST /api/mcp/servers/<name>/refresh`, `PUT /api/mcp/servers/<name>`), state override file `<codex_memory_root>/mcp_state.json` (mode 0600), and a new **Connectors** tab in the Web Console UI. See `docs/mcp.md`.
- **Mobile-friendly Web Console & PWA support** (`CONVEYOR_WEB_MOBILE_UI`, default off): Fluid single-column responsive layout at ≤760px viewport widths with zero horizontal page scroll on 360–430px smartphone screens. Replaces desktop top tabs with an accessible, touch-friendly bottom navigation bar (Tasks, Chat, Approvals with pending count badge, Skills, and a More bottom sheet). Converts Sessions and Context/Changes sidebars into slide-over drawers with touch backdrops and Esc dismissal. Keeps the chat composer sticky above bottom navigation with safe-area insets and 16px textarea font size to prevent iOS auto-zoom. Serves unauthenticated PWA manifest at `/manifest.webmanifest` and web app icons without registering a service worker. See `docs/mobile_web.md`.
- **Provider API key scoping for Codex and Claude Code child processes** (`CONVEYOR_CHILD_ENV_SCOPE_PROVIDER_KEYS`, default off): Automatically filters credential-like environment variables matching provider prefixes (`OPENAI_`, `AZURE_OPENAI_`, `MINIMAX_`, `ANTHROPIC_`, `DEEPSEEK_`) so child processes only receive the active provider's credential key (or Anthropic credentials for `ClaudeCodeBackend`), while preserving non-credential provider configuration and explicitly configured extra prefixes. Logs dropped variable names without disclosing secret values.
- **Skills library v1** (`CONVEYOR_SKILLS_ENABLED`, default off, roadmap P2-4): Reusable operator-authored Markdown procedures that the chat assistant can list, load, and follow on demand, managed in the Web Console. Persisted in SQLite at `<codex_memory_root>/skills.db` (file mode 0600) with validation (slug regex/auto-derivation, single-line constraints, 8000-char body cap, max 100 skills, secrets rejected via `redact_text`, immutable slugs). Chat integration injects a bounded enabled-skills index (≤30 skills, ≤2000 chars) into the system prompt; tools `skill.list` (READ), `skill.load` (READ, wraps body and safety header, escapes `</skill>`, unknown slug lists valid slugs, calls `mark_used`), and `skill.create` (WRITE, requires confirmation, pipe format `name | description | body`, editable in Unified Approval Inbox). Explicit `/skill <slug> [request]` in Web chat and routines injects the wrapped procedure into system context for that turn only and executes the remaining request; unknown slugs reply with available skills without model calls. REST API (`GET/POST /api/skills`, `GET/PUT/DELETE /api/skills/<slug>`, `GET /api/skills/<slug>/export` markdown stream; returns 409 when flag off). Web Console adds a **Skills** tab with search filter, create/edit modal with live char counter, two-step delete, Markdown import/export, and "Use in chat" prefill. See `docs/skills.md`.
- **Unified approval inbox with editable drafts** (`CONVEYOR_APPROVAL_INBOX_ENABLED`, default off, roadmap P2-2): Tool summaries of `email.send` / `github.comment` / `github.create_issue` now state their pipe formats. Aggregated approval inbox in Web Console combining pending tool confirmations (web chat, routines, webhook-triggered runs) and Codex job Apply/Discard approvals. Endpoints `GET /api/approval-inbox` and `POST /api/approval-inbox/<id>/approve|reject` provide centralized review and draft editing before confirmation. Structured draft schemas with validation, secret detection (`redact_text`), and optimistic concurrency checking (`expected_arg`) for editable tools (`email.send`, `github.comment`, `github.create_issue`, `notes.add`, `memory.remember`, `routine.create`). Audit logging with redacted old/new previews (`audit_tool_event`, `action="edited"`). Web Console UI introduces an **Approvals** tab with real-time polling, pending count badge, draft editing with live validation, changed fields summary, and two-click confirmation. See `docs/approval_inbox.md`.
- **Webhook triggers for General Routines** (`CONVEYOR_WEBHOOKS_ENABLED`, default off, P2-5): External systems (GitHub webhooks, curl, external cron) can trigger existing routines with event payloads signed via HMAC-SHA256 using Conveyor-generated secrets (`POST /hooks/<hook_id>`). Requires both `CONVEYOR_WEBHOOKS_ENABLED` and `CONVEYOR_ROUTINES_ENABLED`. Public endpoint enforces 64KB max body, signature verification (`X-Conveyor-Signature` or `X-Hub-Signature-256`), replay protection with 7-day retention (`X-Conveyor-Delivery` / `X-GitHub-Delivery`), concurrency and rate limiting (1 in flight, max 1 accepted per 10s with `Retry-After`), and runs scheduled on the Web Console's asyncio loop with trigger recorded as `webhook`. Untrusted event payload is isolated in `<webhook-event>` tags with prompt injection safety instructions, truncation, secret redaction, and `</webhook-event>` escaping; write tools still require manual approval, and webhook runs get no long-term memory. Rejected (409/429) deliveries stay retryable with the same delivery id. Web Console inbox management API (`POST /api/routines/<id>/hook` create/rotate, `DELETE /api/routines/<id>/hook`) with one-time credential display and curl example in the Web Console UI. See `docs/routines.md`.
- **Durable long-term memory** (`CONVEYOR_LONG_TERM_MEMORY`, default off): explicit remember / forget of one sentence via chat tools (`memory.remember`, `memory.forget`, `memory.list`, `memory.search`). Writes use the existing confirmation flow. Facts persist in `long_term_memory.db` under `codex_memory_root`, are shared across new chats and (by default, `CONVEYOR_LONG_TERM_MEMORY_SHARED=true`) across web / Telegram / Feishu, and only a bounded profile-plus-recent-log slice (plus up to 3 older rows matching the message) is injected. Chinese-aware search (2-character terms). Web Console **Memory** page and authenticated `GET/POST /api/memory`, `DELETE /api/memory/<id>` (delete needs explicit confirm in the UI). Group chats (Feishu `group`, Telegram group/supergroup) get no memory by default — nothing injected, `memory.*` refused (`CONVEYOR_LONG_TERM_MEMORY_GROUPS=true` to allow). Secrets and plain-language credentials are refused. Today's MEMORY.md journal and per-chat `chat_memory.db` are unchanged. See `docs/long_term_memory.md`.

### Fixed
- **Live screen opened on a black frame** when a screensaver had to be dismissed first; the console now asks the screensaver whether it is showing, dismisses it only then, and waits for it to leave the screen before the first capture (its black picture is stable, so comparing consecutive captures was not enough).
- **Host screenshot previews were blocked by the console's own CSP** (`img-src` lacked `blob:`).
- Startup noise: pin the `GdkPixbuf` typelib version in the desktop chat window and silence python-telegram-bot's `per_message=False` notice.
- README roadmaps listed the shipped sentry as upcoming.
- **Live screen after a desktop resize**: the screen size was cached from the first capture, so after the desktop changed resolution clicks were mapped and bounds-checked against the old size. The size now comes from each frame's JPEG header, input is checked against the live display geometry, and a viewer returning after a pause waits for a fresh capture instead of being handed the last frame in memory.
- **`make smoke` no longer grows `/tmp`**: the run gets its own `TMPDIR`, removed on exit. Several smokes leaked `mkdtemp` directories, which a deploy runs twice.
- **Hourly maintenance never passed on a non-MiniMax deployment**: the provider health check required `MINIMAX_API_KEY` unconditionally. It now probes `/models` of whichever provider `~/.codex/config.toml` selects (check name `provider`), and parses the config without `tomllib` so it works on Python 3.10.
- **Command harness was not hermetic**: under systemd it inherited the production `.env`, so a live chat tier rerouted the plain-text case away from the fake runner (and called the real chat API every hour). The harness now drops deployment env and runs against a temporary root.
- **Daily worktrees reported as orphans**: `job_audit` flagged `day-YYYY-MM-DD` worktrees that `reconcile_orphans` deliberately keeps.
- **Deploy inspected the wrong queue database**: `deploy_vps.sh` resolved `~/.codex` as the deploy user, so the idle-queue gate and the pre-deploy backup ran against a stale file. Both now go through `scripts/deploy_db.py` as the service user (needs one sudoers rule, documented in the script header).
- **Feishu card buttons never reached the handler**: the bot registered `card.action.trigger`, which `lark_oapi.channel` rejects as unknown; it now registers `cardAction` and `extract_card_action` accepts the SDK's flattened event.
- **Telegram error handler lost tracebacks** (`NoneType: None`); it now logs `context.error`.
- **Memo classification** falls back to the chat tier endpoint (`CONVEYOR_CHAT_*`) when no MiniMax key is set instead of filing everything as `unfiled`.
- **Log redaction**: Feishu long-connection `access_key` / `ticket` query values are redacted (including the SDK's own `Lark` logger), and the allowed open_id is masked at startup.
- **Stray `<MagicMock ...>` SQLite files** committed by a subagents test are removed and the test no longer creates them; a flaky MCP redaction assertion (`"123"`) uses a distinctive value.
- **`/dev/uinput` udev rules** are `0660` (group `input`) instead of world-writable.
- **Real mouse input via /dev/uinput on non-accessible desktop areas**: Pixel clicks landing on canvases, custom UI elements, or widget-free web regions deliver kernel-level input events through a virtual pointer device (`/dev/uinput` with udev permissions and `xserver-xorg-input-libinput`) rather than synthetic X events that GTK or browsers drop. The xrdp Xorg configuration enables dynamic device discovery (`AutoAddDevices "on"`). Local CUA transport resolves `window_id` from `pid` when needed, ensuring the screenshot's `capture_id` and window-local coordinates route physical clicks to the target window without moving the operator's cursor. Automated with `scripts/setup-vps-uinput.sh` and tracked in `deploy/conveyor-vps-computer.service`.
- **A named follow-up click skips the model**: `再点等号` and `然后再点等号` press the `=` button. The shortcut does not clear the calculator. If that label is missing because the chat window is in front, the next look uses the calculator window. A label that is still missing goes to Codex. Typing follow-ups such as `再输入` stay on Codex.
- **The VPS chat window imports GTK from the system**: the app venv does not include PyGObject. Before importing GTK it appends Ubuntu's `dist-packages`, and the venv's own packages stay first.
- **Running out of desktop steps is a stop**: the task is `stopped` with reason `max_steps reached`. It is no longer stored as `done`.
- **A short follow-up keeps the desktop session**: `再点等号` runs on the desktop when the previous desktop thread is less than 30 minutes old, and that Codex thread is resumed. The first step looks at the screen before asking the model. The planner uses a button label when one exists, and window-local pixels otherwise. Clicking a window's center only raises that window.
- **The VPS chat window shows the latest screenshot**: the reply names the screenshot id, and the window loads that PNG from the local screenshot directory. The reply still has no image bytes.
- **A pixel on a button presses that button**: the point is matched to the smallest clickable accessibility frame from the same window snapshot, and that element's action is used. A point with no frame still uses the window screenshot. A raw pixel the driver cannot deliver is still a failure.
- **A pixel click uses the window screenshot**: x/y on a named window are bound to a fresh `get_window_state` `capture_id`. A screen point inside that window is converted to the screenshot's pixels. The window center still raises the window.
- **Web chat runs a desktop request on the VPS**: a sentence such as “打开计算器” in the web chat box takes the same computer-use route Telegram and Feishu already use. Ordinary chat stays on the chat model. The stream stays open for the desktop task's time limit.
- **Queued approval notifications stay on the real notifier**: a send already handed to the background worker no longer follows a notifier factory installed while it is waiting. That was appending to the next test's fake notifier and failing the relay count checks. Opening the relay database also retries `journal_mode=WAL` when another connection still holds it, instead of failing the publish immediately.
- **A desktop task keeps one Codex session**: the first step starts `codex exec`, and later steps of that same task use `codex exec resume` with that session id. A failed resume starts a fresh session instead of `--last`. A saved screenshot is still attached with `--image`.
- **A labeled button click uses a fresh element token**: the Linux driver rejects `element_index`, and a token from the previous observe is stale in the next process. The click now reads the window and presses the button inside one driver session. The calculator's `1` key is that button.
- **The desktop planner sees the observe screenshot**: when an observe step saved a PNG, the next Codex action call receives that file via `codex exec --image`. A step with no image stays text-only. A single-digit goal whose observe has no accessibility button falls through to that image instead of observing forever. A click inside a listed window's screen rectangle uses desktop coordinates. Naming Calculator, including 计算器, launches it when it is not already running.
- **A window near the screen origin still raises from its desktop center**: the screen center of a window at x=5 also falls inside that window's width and height. That click now calls `bring_to_front` too. A point that is not the screen center stays a normal click.
- **Raising an existing window uses the window manager**: a click at a window's desktop center (the point `x+w/2`, `y+h/2` from the window list) calls `bring_to_front` instead of a window-local click. That click was exiting `screenshot_context_missing` and left the window behind. A later `done` is refused while the newest click, type, hotkey, or scroll failed. Deploy logs when a candidate unit was not active, so a skipped `conveyor-vps-computer` restart is visible.
- **Desktop observe now lists windows, and a failed step does not end the task**: the planner receives each on-screen window (app, a 32-character title, z-order, pid, and bounds) and is told to click an existing app window instead of typing a shell command. type/hotkey/scroll without a pid reuse the window just observed. A driver failure such as a missing pid is recorded and the planner continues. `conveyor-vps-computer` restarts with the rest of the active units on deploy.
- **Natural-language desktop requests use the direct-mode gate**: `computer.task` no longer asks for a second confirmation. A blocked keyword is refused, always-direct off still returns the `/computer_arm` warning, and always-direct on runs the computer-use loop. A host snapshot such as "帮我运行 htop" stays on the ops fast path instead of being swallowed by that desktop verb.
- **Read-only host questions stay off Codex when chat tools are on**: a disk or service-status question that names the operator's machine ("我的服务器磁盘还剩多少") is answered by the existing `disk` / `service_status` tool in one hop. Edit, run, and deploy imperatives still start a Codex job, including "生成磁盘报告脚本", "运行一下", "排查", and "run" / "debug" / "investigate" even when they mention disk. "服务在运行吗" stays on `service_status`. With `CONVEYOR_CHAT_TOOLS` off, machine-naming questions keep the Codex route. "看看磁盘" stays on the deterministic route.
- **Context panel desktop overlap & collapsible changes section**: Neutralized grid column overflow and unconstrained flex headers in the center stream column, keeping `aside.context-panel` securely within its own column without overlapping view tabs or status badges at desktop resolutions down to 1024px. Added collapsible CHANGES section with local storage persistence (`conveyor-changes-collapsed`) and an auto-hiding export toast notice in `SkillsPanel.tsx`.
- **Memory page contrast & refusal responses**: Replaced unstyled and low-contrast inline styling in `MemoryPanel.tsx` with light theme variables and CSS classes in `v2.css`, meeting WCAG AA contrast. Changed `POST /api/memory` fact refusal (secrets/length/sentence structure) from HTTP 400 to HTTP 200 with `{"ok": false, "refused": true, "error": "<reason>"}` so expected refusals do not trigger browser console network errors, while keeping malformed payloads at HTTP 400 and successful additions at 201 with `ok: true`.
- **Approval status lag in the web UI**: a poll that started before a decision could finish afterwards and paint the approval as pending again until the next interval. Refreshes now carry a generation and drop stale responses; the inbox paints the decision immediately; job-event streams also refresh on `approval.*` / `apply.*` / `discard.*`.


## [0.3.0] - Unreleased (draft; tag to be created by the maintainer)

### Fixed
- **Worktree failure cleanup & orphan reconcile**:
  - Automatically remove newly created worktrees when jobs fail or are cancelled without working tree changes (`git status --porcelain` empty), and close any associated active refinement chain.
  - Added `runner.reconcile_orphans` to sweep orphan worktrees older than TTL, exposed via `scripts/reconcile_worktrees.py` and auto-maintain integration.
  - Added `CODEX_BIN` resolution verification to `web_console.py --check`.

### Added
- **Model-requested web search (chat tier)**: with a search backend configured, the chat model can reply `[[SEARCH: <query>]]` when it is not sure or the facts may be recent; Conveyor shows "🔎 搜索：…", runs one search and re-asks with the evidence (one round max, 200-char query cap, control tokens never streamed). A failed search marks the answer unverified. Keyword pre-fetch for time-sensitive questions stays. Smoke: 3 new cases in `scripts/chat_tier_smoke.py`.
- **Cross-restart persistent conversation memory**: `chat_memory.db` SQLite store under `settings.codex_memory_root` persists active conversation turns, session state, and `/deep` escalation requests across bot restarts. Commands `/chat_clear` and `/forget` reset active history in memory and database. Smoke: `scripts/chat_memory_smoke.py`.
- **Proactive topic watch & push notifications**: `/watch <topic> [hours]` subscribes to periodic web search monitoring with scheduler checks (`scripts/scheduler_tick.py`), SHA-256 content diffing against previous run digests, and proactive push notifications to Telegram/Feishu channels with verified sources only. Commands `/watches` and `/unwatch <id>`. Smoke: `scripts/topic_watch_smoke.py`.
- **Chat tier / intent mode** (`CONVEYOR_CHAT_MODE=auto`, default `off`): conversation, Q&A, quote and image questions are answered by a direct, streamed OpenAI-compatible chat-model call with no tools (`runner/chat_client.py`, `handlers/chat.py`); clear execution requests and anything about the operator's own systems go straight to Codex, and the model escalates with `[[ESCALATE]]` when it needs tools. Hallucination guards: evidence-first for time-sensitive questions, unverifiable links removed by a code check, self-graded `[[CONFIDENCE]]` with a low-confidence flag, "unverified" marking for fresh facts without search, no-fake-actions rule, per-answer log line. `/deep` (and a Telegram button) re-runs the last chat request on Codex; escalations after reading untrusted quotes/images wait for `/deep` confirmation, and Codex always receives the operator's own words. Short-term per-chat history (30 min). Any chat failure falls back to Codex. Smoke: `scripts/chat_tier_smoke.py` (17 cases, fake SSE server).
- **Mention & reply context (Grok-style)**: reply to or quote any message and mention the bot to fact-check (`这是真的吗` / `is this true?` — web evidence pack + ✅/❌/⚠️/❓ verdict with sources, degrading to agent-only checking without a search backend), explain (bare mention), summarize, translate or ask about it. `记一下` on a reply saves the quoted text; `/run` and `/fix` on a reply carry it as context; replying to the bot's own answer continues the thread. Quoted text is wrapped as untrusted data (tag-closing neutralized, 3000-char cap) and never drives intent routing. `channel/types.ReplyContext`, `channel/mentions.py`, `handlers/context.py`, `personal_tools.research.factcheck_evidence`; see `docs/reply_context.md`. Smoke: `scripts/reply_context_smoke.py` (26 cases).
- **Image understanding**: photos and image files (Telegram photo / image document, Feishu image and post images) sent to the bot, or on the replied-to message, are downloaded after the allowlist check into a private store (`runner/attachments.py`: magic-byte check, random `0600` names, 10 MB / 4-image caps, 7-day retention) and attached to the agent job — `codex exec --image`, or `--add-dir` for the Claude Code backend. A captionless photo is explained; captions are the question (and may be `/fix …`); quote + image fact-checks work together. Only leading `[image: <random-name>]` prompt headers attach files and only from the store, so quoted text cannot attach arbitrary files. Smoke: `scripts/image_context_smoke.py` (18 cases).
- **Group chat gating**: Telegram groups now act only when the bot is @mentioned (or text-mentioned) or replied to, with the bot's `@username` stripped; unauthorized group members are ignored silently instead of receiving "Unauthorized.". Telegram forum-topic pseudo-replies are not treated as reply context.

### Fixed
- **Feishu group mentions**: `mentioned_bot` is now derived from the event's mention list and the bot's `open_id` (the SDK leaves the flag unset for message events, so group @mentions were previously dropped), and the bot's `@name` is stripped so `@bot /status` parses as a command.

### Fixed
- **Direct Computer Use Fix Pack (P5.6.2)**:
  - Accept Cua success metadata `active_app` / `click_method` in step results (was `invalid_result`).
  - Enforce `CONVEYOR_COMPUTER_DIRECT_ENABLED` for arm/task/action and `is_direct_mode_active`; `ALWAYS_DIRECT` cannot bypass a disabled DIRECT flag.
  - Register `/computer_observe` and `/computer_action` slash commands to match docs.
  - CodexPlanner AX-first prompt; observe attaches `pid` / `window_id` / short `element_hints` for the planner.
  - App allowlist: resolve target app from AX `pid` (not only frontmost); observe/wait skip allowlist (blocklist still applies).
  - With a non-empty app allowlist, reject bare x/y clicks (`ax_required_when_app_allowlist_set`).
  - Trajectory dirs `0700` / JSONL `0600` best-effort; `/computer_status` shows USE + DIRECT flags.
  - Smokes expanded in `scripts/desktop_computer_smoke.py` (33 cases).
  - Single-digit click goals (e.g. 点击数字 1) use a deterministic path:
    observe → optional Clear → one digit AX click → done (avoids Codex multi-click thrash).

### Added
- **Direct Computer Use Hardening (P5.6.1)**:
  - **AX-First Click Preference**: Prioritizes AX/element clicks over coordinate-based clicks if both options are present. Falls back to coordinates if needed, and logs the click method used (`ax_click` vs `xy_click`).
  - **App Allowlist/Blocklist Validation**: Restricts task execution based on the active application. Added `conveyor_computer_allowed_apps` and `conveyor_computer_blocked_apps` settings (default blocks Keychain Access, System Settings, Terminal). Active app name is polled on macOS via AppleScript.
  - **Structured JSONL Trajectories**: Appends step records including timestamp, task_id, step index, screenshot_id/hash, action type, redacted args, result status, error, and duration_ms to `codex_memory_root/computer/trajectories/<task_id>.jsonl` with full redaction of typed text.
  - **Concise Failure Cards**: Formats concise failure summaries containing task ID, stop reason, last action, last screenshot ID/hash, steps completed, and log recommendations when a task fails or hits limits.
  - **Telegram Stop Fast Path**: Routes natural language stop commands (`停下`, `别动`, `停止操作`, `stop computer`, `cancel computer task`) directly to the `computer.stop` tool at dispatch time to bypass Codex routing delays.
  - **Heartbeat & Status Upgrades**: Heartbeats now track the `poll_computer` option. `/computer_status` displays Cua command, availability, version, permissions, allowed/blocked apps, agent heartbeats, and active task details.
  - **Smoke Tests**: Added 6 tests to `scripts/desktop_computer_smoke.py`, verifying all status fields, JSONL logs, failure reports, AX click preferences, app restrictions, and the stop fast path.

- **Persistent Job Queue (P4.4)**:
  - Persistent SQLite DB store under `codex_memory_root/state/job_queue.sqlite3` with columns for ID, operator, channel, mode, prompt, state, timings, errors, and metadata.
  - Queue `queued`/`running` states and `paused` status persist across bot restarts and VPS reboots.
  - Startup recovery: automatic transition of any previously `running` jobs to `interrupted`.
  - Background auto-resume of `queued` jobs on startup if bot is configured.
  - Exception handling: mark running queue row as failed if job fails to start.
  - Updated `/queue_clear` command to directly cancel all queued jobs and aligned the help text.

- **Direct Computer Use Mode (P5.6, cua backend, OFF by default)**:
  - Hands-free loop: `Telegram/Feishu NL → Codex → Conveyor computer-use tools → Mac desktop_agent → local cua-driver → real desktop actions`. Cua (`trycua/cua`) runs **only on the Mac agent**; the VPS never speaks the Cua protocol.
  - Config flags (all default safe/off): `CONVEYOR_COMPUTER_USE_ENABLED`, `CONVEYOR_COMPUTER_DIRECT_ENABLED`, `CONVEYOR_COMPUTER_ALWAYS_DIRECT`, `CONVEYOR_COMPUTER_MAX_STEPS` (20), `CONVEYOR_COMPUTER_MAX_SECONDS` (600), `CONVEYOR_CUA_DRIVER_CMD` (`cua-driver mcp`), `CONVEYOR_COMPUTER_ALLOWED_ACTIONS`, `CONVEYOR_COMPUTER_BLOCKED_KEYWORDS`, `CONVEYOR_COMPUTER_BACKEND` (`http`/`fake`).
  - Commands: `/computer_status`, `/computer_arm [minutes]` (TTL arm), `/computer_task <goal>`, `/computer_stop` (kill switch), `/computer_log [task_id]`, `/computer_screenshot`, `/computer_observe`, `/computer_action <json>`.
  - `nodes/types.py`: new `CAP_COMPUTER_USE_DIRECT` + `DESKTOP_DIRECT_CAPABILITIES`; `nodes/registry.py` `computer_use_active()` kill-switch predicate (project-level `is_stub_environment()` master switch untouched).
  - `desktop_computer_requests.py`: file-backed task/step store at `CODEX_MEMORY_ROOT/state/desktop_computer_requests.json` (cross-process lock + atomic write) with arm TTL, direct-mode gating, blocked-keyword guard, action allow-list, redaction.
  - `desktop_cua.py`: `CuaDriver` with `LocalCuaTransport` (locates the configured local `cua-driver` binary, executes via `cua-driver call <tool> <json>`, redacts logs, stores Cua screenshots locally as metadata-only ids) and `FakeCuaTransport` (test-only, never retains plaintext).
  - `desktop_agent_server.py`: new `/desktop/computer/task/step/claim|complete|fail`, `/desktop/computer/task/pending`, `/desktop/computer/status` endpoints.
  - `desktop_agent.py`: new `--poll-computer` loop executing claimed steps locally via `build_driver`.
  - `handlers/tools/executors.py`: `computer.status` (READ), `computer.observe` (READ), `computer.action` (WRITE_SAFE), `computer.task` (WRITE), `computer.stop` (WRITE_SAFE).
  - `handlers/intent.py`: `_COMPUTER_TASK_PATTERNS` (操作电脑 / 帮我点 / 打开 <app> / 在电脑上 / control my mac …) now route to `computer.task`; status phrases stay on `computer.status`.
  - Hard safety envelope enforced even in direct mode: action allow-list, blocked-keyword guard, no secret injection, typed-text/hotkey redaction in all logs, driver result allow-list, `MAX_STEPS`/`MAX_SECONDS` caps, `/computer_stop` kill switch. Cua never crosses the network.
  - Smoke: `scripts/desktop_computer_smoke.py` (14 cases) added to `make smoke`, including Cua CLI wrapper mapping, current `get_desktop_state` fallback, arm/expiry, blocked keyword, max steps, stop, and redaction boundaries. `scripts/cua_driver_real_smoke.py` adds a read-only Mac-side verifier for an installed/authorized driver. See `docs/desktop_security.md §7`.


## [0.1.1] - 2026-07-06 (Security Hardening)

### Security Hardening & Fixes
- **Redaction coverage expanded**: Expanded secret redaction patterns to prevent token/key leakage in logs.
- **Exception/stderr/job error redaction**: Hardened `SecretRedactingFilter` to redact exception text, clear `exc_info`, and redact stderr, job errors, and start-failed exceptions before user-facing display.
- **Child env secret stripping**: Stripped sensitive application secrets (like bot tokens, app passwords, search keys) from Codex child process execution environment.
- **Desktop observe path validation**: Validated desktop observe result paths under the screenshots directory.
- **Systemd ReadWritePaths narrowing**: Narrowed systemd `ReadWritePaths` configuration from broad `/home/ubuntu`.
- **Security audit permission checks**: Registered `queue.status` as a `READ` tool, checked security audit parameters.
- **Apply policy high-risk hardening**: Hardened apply policy checks for high-risk files.
- **Security regression smoke suite**: Added `scripts/security_regression_smoke.py` to `make smoke`.
- **NL router fixes**: Fixed `queue.status` and `nl_support` propagation in the NL router.

### Added

#### Natural Language Agent Router (P4.3)
- Natural-language-first routing: users can invoke most tools with normal language
- Slash commands remain as precise fallback/debug commands
- Unified tool catalog built from host + personal tool registries
- Tool catalog includes: name, summary, danger level, keywords, examples, domain, nl_support
- `/nl_help` command: lists NL examples grouped by domain with honest support tags
- Extended NL coverage: notes search, reminders create, calendar freebusy, queue status, setup status
- Clarification messages use natural language (no slash format suggestions)
- Safety: WRITE/DESTRUCTIVE tools never auto-execute from NL
- WRITE_SAFE tools (notes.add, reminders.create) audited when triggered by NL

#### NL Router Polish (P4.3.1)
- Renamed NL categories: WRITE_SAFE_AUTO for low-risk audited actions, WRITE_CONFIRM_PREVIEW for WRITE/DESTRUCTIVE
- Added `queue.status` READ tool: routes "队列状态" to job queue status (not scheduler_status)
- `scheduler_status` reserved for "调度器状态" (reminder scheduler)
- `/nl_help` now shows honest support tags: [自动], [需确认], [会追问], [示例]
- Support tag legend explains what each tag means
- 28 smoke tests covering catalog, routing, safety, categories, and /nl_help honesty

#### NL Router Final Polish (P4.3.2)
- `queue.status` registered in host TOOL_REGISTRY (previously only in personal tools)
- `_build_catalog` now correctly propagates `nl_support` from `_DOMAIN_DEFS` to `ToolCatalogEntry`
- `/nl_help` support tags now accurately reflect tool capabilities
- 35 smoke tests covering all P4.3.1 + new queue.status registry, routing, and nl_support propagation

#### File Search / Knowledge Base (P4.2)
- Natural-language-first file search with automatic READ-only fact collection
- File search with strict safety boundaries (only configured roots allowed)
- Knowledge Base with SQLite FTS5 for fast full-text search
- Rejects sensitive files (.env, secrets/, .ssh/, private keys, tokens, binary files)
- Commands: /files_roots, /files_search, /files_read, /kb_index, /kb_status, /kb_search, /project_docs
- Config: FILE_SEARCH_*, KB_ROOT, KB_INDEX_PATH settings
- Natural language routing: "找一下文档里关于 deploy 的说明", "README 里有没有 Gmail 配置", "根据本地文档总结安装流程"
- Auto fact collection for hybrid synthesis

#### Web Search + Research (P4.1)
- Web Fetch MVP: READ-only curl wrapper with strict URL validation
- URL validation rejects localhost, private IPs, metadata endpoints
- Web Search with multi-backend support (disabled, searxng, brave, tavily, serper)
- Research tool: hybrid web.search + fetch + Codex synthesis
- Project research: uses project context for better search results
- Commands: /web_fetch, /web_text, /web_headers, /web_search, /research, /project_research
- Config: WEB_FETCH_*, WEB_SEARCH_*, RESEARCH_* settings
- Natural language routing: "搜索 Python asyncio", "研究一下 AI 编程助手", "获取网页 https://example.com"

### Fixed

#### Web Search + Research Hardening (P4.1.1)
- **API key safety**: Replaced curl subprocess with urllib.request to avoid exposing API keys in process argv
- **Redirect safety**: Disabled automatic redirects (--no-location), each hop must be validated
- **Content-Type validation**: Only allows text/*, application/json, application/xml on both HEAD and GET
- **IP blocking**: Expanded blocked ranges to include 100.64.0.0/10 (carrier-grade NAT), 198.18.0.0/15 (benchmark), multicast (224.0.0.0/4), reserved (240.0.0.0/4), IPv6 link-local (fe80::/10)
- **Metadata endpoint**: Explicit blocking for 169.254.169.254 and metadata.google.internal
- **WEB_SEARCH_ENDPOINT validation**: Rejects localhost/private/link-local/metadata endpoints
- **URL encoding**: Search queries are properly URL encoded for all backends
- **Research behavior**: /research and /project_research now use Codex hybrid synthesis
- **Redaction**: WEB_SEARCH_API_KEY never appears in errors, repr, audit, or chat output

## [0.1.0] - 2026-06-17

### Added

#### Core (P1-P2)
- Telegram bot with single-operator Codex CLI runner
- Feishu/Lark bot as second channel
- Session summary for "继续" / "continue" context
- Progress mode (verbose/compact/quiet) for streaming UX
- Memory system (记 xxx → MEMORY.md categorization)
- Codex job queue with single-concurrency FIFO
- Host ops commands (/load, /vps, /htop, /ps, /disk)

#### Personal Tools (P3.1-P3.2)
- Notes system (add, search, list, delete)
- Reminders with creation, listing, cancellation, due checks
- Scheduler for reminder delivery
- Personal tools SQLite store with operator isolation

#### Gmail Integration (P3.3)
- Gmail App Password backend (IMAP + SMTP)
- Commands: /gmail_status, /gmail_recent, /gmail_search, /gmail_read
- Email sending via /email_send

#### Google OAuth (P3.4)
- Google OAuth for Calendar and Contacts
- Commands: /auth_google, /google_status
- Calendar: /calendar_today, /calendar_tomorrow, /calendar_week, /calendar_search, /calendar_freebusy, /calendar_create
- Contacts: /contacts_search

#### Daily Briefing (P3.5)
- Daily briefing system aggregating Calendar, Reminders, Gmail, Notes
- Commands: /brief_today, /brief_tomorrow, /brief_settings, /brief_enable, /brief_disable, /brief_probe
- Briefing settings persistence per operator

#### GitHub Integration (P3.6)
- GitHub Issues/PRs/CI read-only tools
- Commands: /github_status, /github_issues, /github_issue, /github_prs, /github_pr, /github_ci, /github_create_issue, /github_comment

#### Natural Language Planner (P3.7)
- Planner profiles composing deterministic tools
- Profiles: daily_priority, dev_plan, project_health, inbox_triage, schedule_review
- Commands: /plan_today, /plan_dev, /planner_health, /inbox_triage, /schedule_review, /planners

#### Codex Job Queue (P3.8)
- Single-concurrency FIFO queue for Codex jobs
- Queue management: /queue, /queue_cancel, /queue_clear, /queue_pause, /queue_resume

#### Generic Project Profiles (P3.9)
- Project profile CRUD with operator isolation
- Project types: generic, mobile_app, web_app, bot, library, research, course, business
- Analysis tools: /project_status, /project_health, /project_roadmap, /project_next, /project_release_checklist, /project_brief
- Commands: /projects, /project_add, /project_use, /project_show, /project_remove

#### Setup Wizard (P3.10)
- Configuration status overview: /setup
- Setup checklist: /setup_check
- Setup guides: /setup_project, /setup_gmail, /setup_google, /setup_github
- Systemd timer checks in /setup_check

#### Project Import/Export (P3.11)
- Export projects as JSON: /project_export [id], /project_export_all
- Import projects from JSON: /project_import
- Project templates: /project_template
- Schema: conveyor.project.v1
- Preserves enabled field on import
- Duplicate name protection

#### Deployment
- Complete .env.example with all settings
- One-click install script: scripts/install.sh
- Systemd units for all services (telegram, feishu, maintain, scheduler)
- Systemd timers for scheduler and maintenance
- Quick start guide (10 minutes)

### Security
- All sensitive fields redacted in logs and output
- Operator-scoped data isolation
- Danger levels for all tools (READ, WRITE_SAFE, WRITE, DESTRUCTIVE)
- Audit logging for dangerous operations
- No token/secret leakage in exports

### Testing
- 45+ smoke tests covering all features
- No network calls in smoke tests
- Redaction verification in all outputs

[Unreleased]: https://github.com/mammut001/conveyor/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/mammut001/conveyor/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/mammut001/conveyor/releases/tag/v0.1.0
