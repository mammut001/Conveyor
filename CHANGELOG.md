# Changelog

All notable changes to Conveyor will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
