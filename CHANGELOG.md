# Changelog

All notable changes to Conveyor will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/mammut001/conveyor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mammut001/conveyor/releases/tag/v0.1.0
