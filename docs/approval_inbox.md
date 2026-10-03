# Unified Approval Inbox with Editable Drafts (Roadmap P2-2, v1)

Conveyor's Unified Approval Inbox provides a single control panel for reviewing, editing, and deciding pending actions across Web Chat, scheduled routines, webhook-triggered routine runs, and Codex job worktrees.

---

## 1. Feature Flag & Configuration

The feature is opt-in and disabled by default.

- **Environment Variable**: `CONVEYOR_APPROVAL_INBOX_ENABLED=true`
- **Config Setting**: `Settings.approval_inbox_enabled: bool = False`
- **System Status**: Reported in `WebControl.system_status()` under `features.approval_inbox`.
- **Default (Flag OFF)**:
  - `GET /api/approval-inbox` and `POST /api/approval-inbox/<id>/approve|reject` return `409 Conflict` with `{"error": "approval inbox is disabled (set CONVEYOR_APPROVAL_INBOX_ENABLED=true)"}`.
  - The Web Console UI does not render the **Approvals** tab.
  - Existing endpoints (`GET /api/approvals`, `POST /api/approvals/<id>/approve|reject`) continue functioning unchanged.

---

## 2. Listed Items & Sources

The inbox aggregates pending approvals on channel `web`, ordered newest first:

1. **Tool Confirmations** (`handlers.tools.confirm.list_pending(channel="web")`):
   - **Chat**: Initiated during interactive Web Console chat (`chat_id` = session identity).
   - **Routine**: Initiated by a scheduled routine run (`chat_id` = `routine-<id>`). Persisted in `routine_approvals` to survive server restarts.
   - **Webhook**: Initiated by an external webhook firing a routine (`routine_runs.trigger == 'webhook'`).
   - Fields: `id`, `kind: "tool"`, `source`, `tool_name`, `summary`, `danger`, `arg`, `draft`, `editable`, `created_at`, `expires_at`, `session_id`, `routine_id`, `routine_name`.
2. **Job Approvals** (`WebControl.list_approvals()`):
   - Initiated by Codex jobs for `/apply` or `/discard`.
   - Fields: `id`, `kind: "job"`, `source: "job"`, `action: "apply"|"discard"`, `job_id`, `created_at`, `expires_at`, `editable: false`, `draft: null`.

### Redaction and Editability
- Arguments and draft fields are inspected using `redact_text` before returning to the authenticated operator.
- If redaction alters the argument (`redacted: true`), editing is disabled (`editable: false`, `draft: null`) to prevent writing redacted placeholders back into system commands.

---

## 3. Editable Tool Allowlist & Draft Schemas

Draft editing is restricted to an explicit allowlist of tools with well-defined schemas. Non-allowlisted tools can only be approved as-is or rejected (`editable: false`).

| Tool Name | Draft Fields | Argument Format |
| :--- | :--- | :--- |
| `email.send` | `to`, `subject`, `body` | `to \| subject \| body` |
| `github.comment` | `number`, `body` | `number \| body` |
| `github.create_issue` | `title`, `body` | `title \| body` |
| `notes.add` | `text` | `text` |
| `memory.remember` | `text` | `text` |
| `routine.create` | `cron`, `prompt`, `name` | `cron \| prompt \| name` |

### Validation Rules
- **Type Checking**: Every draft field must be a string.
- **Single-Line Fields**: `to`, `subject`, `number`, `title`, `cron`, `name` must not contain pipes (`|`) or newlines (`\n`, `\r`).
- **Prompt Field**: `prompt` must not contain pipes (`|`) (newlines are permitted).
- **Number Field**: `number` must contain digits only (`^\d+$`).
- **Recipient Field**: `to` must consist of valid comma-separated email addresses.
- **Length Caps**:
  - `to`: max 320 chars
  - `subject`, `title`: max 200 chars
  - `name`: max 80 chars
  - `body`: max 20,000 chars
  - `text`, `prompt`: max 4,000 chars
- **Secrets Screening**: Drafts cannot contain secrets or tokens detected by `redact_text`. Violations fail with `"drafts cannot contain secrets or tokens"`.
- **Memory Screening**: `memory.remember` drafts must pass `personal_tools.long_term_memory.screen_write_arg` (single sentence, no credentials).
- **Cron Validation**: `routine.create` cron expressions must pass `routines.validate_cron`.
- **Round-Trip Consistency**: `build_arg(tool_name, parse_draft(tool_name, arg)) == arg` holds for all well-formed arguments.

---

## 4. Editing, Concurrency & Optimistic Locking

### Atomicity & Persistence
- `approval_inbox.edit_pending(settings, token, draft)` updates the pending action's `arg` under the in-memory confirm store's reentrant lock (`replace_pending_arg`).
- It preserves `token`, `tool_name`, `channel`, `chat_id`, `operator_id`, `created_at`, and `ttl_seconds`.
- If the action originated from a routine and was persisted in SQLite (`routine_approvals`), the database row is atomically updated as well.
- An audit record is logged via `handlers.tools.audit.audit_tool_event` with `action="edited"`, capturing redacted previews of both the old and new arguments.

### Optimistic Concurrency Check (`expected_arg`)
- When submitting an approval with or without draft edits, the client may provide `expected_arg`.
- If `expected_arg` does not match the server's current pending argument, the endpoint returns `409 Conflict` (`{"error": "draft changed, reload"}`), preventing lost updates if concurrent edits or background processes alter the action.

---

## 5. API Endpoints

All endpoints require Bearer authentication. When `CONVEYOR_APPROVAL_INBOX_ENABLED=false`, all endpoints return `409 Conflict`.

### `GET /api/approval-inbox`
Returns pending approval items and source counts:
```json
{
  "items": [ ... ],
  "counts": {
    "total": 3,
    "chat": 1,
    "routine": 1,
    "webhook": 1,
    "job": 0
  }
}
```

### `POST /api/approval-inbox/<id>/approve`
Body: `{}` or `{"draft": { ... }, "expected_arg": "..."}`
- **Tool Items**:
  - Validates `expected_arg` if supplied.
  - If `draft` is supplied, applies `edit_pending` (returns `400 Bad Request` if invalid or non-editable).
  - Executes approval through `web_chat.decide_tool_approval`, updating session transcripts and routine records.
- **Job Items**:
  - Decides job approval via `WebControl.decide_approval`. Supplying a `draft` for a job approval returns `400 Bad Request`.
- **Status & Errors**:
  - Missing or expired actions return `404 Not Found`.
  - Second approval on an already-decided action returns `404 Not Found`.

### `POST /api/approval-inbox/<id>/reject`
Body: `{}`
- Resolves the tool action or job approval as rejected.
- Returns decision status and redacted result text.
- `expected_arg` is an optimistic check: it must equal the current pending arg, or its redacted form (what the list showed). Otherwise 409 `draft changed, reload` and nothing runs.

---

## 6. Web Console UI (`ApprovalInboxPanel.tsx`)

- **Tab Display**: The **Approvals** tab is rendered in the top navigation bar when `features.approval_inbox` is enabled.
- **Pending Count Badge**: Automatically polls `GET /api/approval-inbox` every 5 seconds while visible, displaying total pending approvals.
- **Card View**: Displays source badges (Chat, Routine, Webhook, Job), tool summary, routine name, timestamps, and formatted read-only fields.
- **Edit Workflow**:
  - Clicking **Edit** opens editable fields with live client-side validation hints.
  - Clicking **Approve edited** displays a compact summary of modified fields (`field: old -> new`).
  - Requires a second click (**Confirm approve edited**) to submit.
- **Reject Safety**: Rejecting requires a two-click confirmation (**Reject** -> **Confirm reject**).
- **Webhook Warning**: Items triggered by external webhooks display a prominent caution banner: `⚠️ Triggered by an external webhook - review carefully`.

---

## 7. Out of Scope for v1

- **Telegram & Feishu Bot Pending Actions**: Confirmations generated directly in private Telegram or Feishu bot chats reside in those bot processes' memory stores and are decided via in-channel inline buttons / text keywords. They are not aggregated into the Web Console inbox in v1.

---

## 8. Security Summary

1. **Immutable Scope**: Draft editing can **only** alter `arg`. It cannot modify the tool name, operator identity, chat context, or channel, and cannot instantiate new actions.
2. **Draft Screening**: All draft inputs undergo secret screening via `redact_text`, preventing accidental injection of tokens or credentials.
3. **No Redaction Overwrites**: Items whose initial arguments contain redacted material are locked from editing (`editable: false`).
4. **Audit Trail**: Every edit is recorded in `audit/tools.log` with redacted old and new argument previews.

## Tool argument formats

`email.send`, `github.comment` and `github.create_issue` summaries (shown to the chat model as the tool description) now spell out their pipe formats (`<to> | <subject> | <body>`, `<number> | <body>`, `<title> | <body>`). Without them the model sometimes produced `to: ...\nsubject: ...` arguments that the tools cannot parse and the inbox could not offer as an editable draft.
