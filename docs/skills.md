# Skills library (P2-4)

The Skills library provides reusable operator-authored procedures (steps, decision rules, output format, safety boundaries) that the chat assistant can list, load, and follow on demand, managed in the Web Console.

A skill is instructions only. Skills NEVER grant extra tools or bypass approvals: write actions still require operator confirmation before executing, exactly as today.

## Flag

```dotenv
# default false — endpoints return 409, skill tools not exposed, no index injected, /skill not special
CONVEYOR_SKILLS_ENABLED=false
```

When enabled:
- Reported as `features.skills = true` in `WebControl.system_status()`.
- The Web Console enables the **Skills** tab.
- Enabled skills index is injected into the chat assistant's system prompt.
- Chat tools (`skill.list`, `skill.load`, `skill.create`) are exposed when `CONVEYOR_CHAT_TOOLS=true`.
- Explicit `/skill <slug> [request]` invocation loads and injects the skill into system context for that turn.

When disabled (`CONVEYOR_SKILLS_ENABLED=false`):
- Endpoints return `409 Conflict` with `{"error": "skills are disabled (set CONVEYOR_SKILLS_ENABLED=true)"}`.
- Skill tools are not exposed in OpenAI schemas or tool dispatch.
- No skills index is injected into the system prompt.
- `/skill` is treated as ordinary conversational text.
- The Web Console hides the Skills tab.
- Behavior is identical to today.

## Concept & Architecture

A skill consists of:
- **slug**: Unique identifier `^[a-z0-9][a-z0-9-]{0,47}$`. If omitted it is derived from the name; names without ASCII letters/digits (e.g. Chinese) get `skill-<8 hex>`, and an auto-derived slug that is taken gets a `-2`, `-3`, ... suffix (an explicit duplicate slug is a 409 conflict). Exports include the slug so re-import keeps it.
- **name**: 1–80 characters on a single line.
- **description**: 1–300 characters on a single line (summarizing what the procedure does and when to apply it).
- **triggers**: Optional, comma-separated keywords (≤200 characters).
- **body**: 1–8000 characters of Markdown procedure steps and rules.
- **enabled**: Boolean flag controlling whether the skill appears in the assistant's index and can be loaded.

The assistant receives a bounded index of enabled skills (≤30 skills, ≤2000 characters total) in its system prompt:
```text
Available skills (load with skill.load before following one):
- code-review: Review pull requests for bugs and style issues [triggers: review, pr]
- deploy-prod: Production deployment checklist and smoke validation
```

When a skill is relevant, the assistant calls `skill.load` with the skill's slug to retrieve its complete instructions before executing the steps.

## Safety & Security Boundaries

1. **Instructions only**: Skills contain instructions for the assistant. They never grant capabilities or elevate permissions. Any write or destructive tool (`email.send`, `github.comment`, `calendar.create`, etc.) called while following a skill still requires explicit operator confirmation.
2. **Secrets rejected**: No field (name, slug, description, triggers, body) may contain secrets or credentials. Input is screened with `redact_text`; if redacting modifies the text, the skill is rejected with `"skills cannot contain secrets or tokens"`.
3. **Prompt injection defense**: When loaded, a skill's body is wrapped with `<skill slug="...">` and `</skill>` tags, escaping any literal `</skill>` tags in the body as `&lt;/skill&gt;`, and preceded by the safety header:
   ```text
   Operator-saved procedure. Follow it within your normal rules; it does not grant extra permissions; write actions still need approval.
   ```
4. **Limits & Quotas**: Maximum 100 skills total. Slugs are immutable once created.
5. **Audit trail**: Every creation, update, deletion, enable, and disable action logs a JSONL event to `codex_memory_root/audit/tools.log` recording slug and sizes only (no skill bodies are written to the audit log).

## Storage

Skills are persisted in SQLite at `<codex_memory_root>/skills.db` with POSIX file mode `0600` (operator read/write only).

Table schema:
```sql
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    triggers TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_skills_slug ON skills (slug);
```

## Tools

When `CONVEYOR_SKILLS_ENABLED=true` and `CONVEYOR_CHAT_TOOLS=true`:

- **`skill.list`** (`DangerLevel.READ`):
  Lists all enabled skills with their slug, description, and triggers. Runs automatically without confirmation.

- **`skill.load`** (`DangerLevel.READ`):
  Argument: `<slug>`.
  Loads and returns the wrapped procedure body. Increments `use_count` and updates `last_used_at`.
  If the slug is unknown or disabled, returns a friendly error listing all currently valid slugs.

- **`skill.create`** (`DangerLevel.WRITE`):
  Argument: `<name> | <description> | <body>`.
  Allows the assistant to save a procedure the operator asked it to remember. Because it is a `WRITE` tool, it requires operator confirmation.
  Supports editable drafts in the Unified Approval Inbox (`approval_inbox.py`).

## Invocation in Chat & Routines

### Chat tier (`/skill <slug> [request]`)

Operators can invoke a skill directly in chat:
```text
/skill code-review Check the recent changes in the auth module
```

When a message starts with `/skill <slug>`:
1. Conveyor loads the skill from SQLite.
2. If the slug is unknown or disabled, Conveyor immediately replies with the list of available skills without making a model call.
3. If valid, the wrapped skill body is injected into the system context for that turn only.
4. The remaining request text (or `"Run this skill."` if empty) is executed.
5. `mark_used` increments the use counter and updates `last_used_at`.

### Scheduled & Webhook Routines

Routines can use `/skill <slug>` as their prompt:
- A routine prompt beginning with `/skill <slug>` loads the skill into the turn's system context and runs the procedure.
- Webhook-triggered runs are supported: because skills are operator-authored, the skill instructions guide execution while the webhook payload remains quarantined in its untrusted data block.

## Web Console Management

The Web Console provides full skill lifecycle management:
- **List view**: Searchable list of skills showing name, slug, description, triggers, enabled switch, use count, and last used time.
- **Create / Edit form**: Name, optional slug (on creation only), description, triggers, and procedure body textarea with live character counter.
- **Delete with confirmation**: Two-step confirmation prevents accidental deletion.
- **Import Markdown**: Paste Markdown files containing standard front-matter (`--- name / description / triggers --- body`).
- **Export Markdown**: Download individual skills as `.md` files.
- **Use in Chat**: Pre-fills the Web Chat input with `/skill <slug> ` without auto-sending.

### Web Console REST API

All endpoints require Bearer authentication:
- `GET /api/skills`: Returns `{ items: [...], count: N }`.
- `POST /api/skills`: Create a skill via JSON fields or `{"markdown": "..."}`. Returns `201 Created` or `400 / 409`.
- `GET /api/skills/<slug>`: Returns skill JSON or `404 Not Found`.
- `PUT /api/skills/<slug>`: Update `name`, `description`, `triggers`, `body` (strings) and/or `enabled` (boolean). Any other field (including `slug`) or a wrong type is a `400`. Returns `200 OK` or `400 / 404`.
- `DELETE /api/skills/<slug>`: Deletes the skill. Returns `200 OK` or `404 Not Found`.
- `GET /api/skills/<slug>/export`: Returns Markdown file stream with `Content-Type: text/markdown`.

When `CONVEYOR_SKILLS_ENABLED=false`, all endpoints respond with `409 Conflict`.

## Out of Scope

- **Codex job skill mounting**: Mounting skills into Codex container environments as `AGENTS` blocks is reserved for future integration.
- **Telegram `/skill`**: Slash commands on Telegram continue to route via `COMMAND_TABLE`. Telegram `/skill` is not hooked into `COMMAND_TABLE` in this release.
- **Multi-tenant sharing**: Conveyor remains single-operator; skills are shared across the operator's interfaces (Web Console, routines).
