# Cross-Channel Approval Relay (Roadmap P2-2 "Telegram/飞书/Web 三端同步")

The Cross-Channel Approval Relay enables a personal-assistant-style "approve from wherever I am" workflow across Conveyor's separate processes (`web_console.py`, `bot.py`, and `feishu_bot.py`).

Every pending dangerous tool action is synchronized across all configured surfaces. The first decision wins everywhere, the action still executes in the origin process owning the tool context, and the other channels are updated to display the final outcome.

---

## 1. Feature Flag & Configuration

The approval relay requires the unified approval inbox to be enabled.

- **`CONVEYOR_APPROVAL_RELAY_ENABLED`**: Boolean (`true`/`false`), default `false`. Only effective when `CONVEYOR_APPROVAL_INBOX_ENABLED=true` (logs a warning and remains disabled otherwise).
- **`CONVEYOR_APPROVAL_RELAY_CHANNELS`**: Comma-separated list of target notification channels to fan out to, e.g. `telegram,feishu` or `telegram`. Default empty (relay synchronizes Web Inbox and bot processes, but sends no outbound push messages).
- **`CONVEYOR_APPROVAL_RELAY_DB`**: Optional path to SQLite relay database. Defaults to `approval_relay.db` inside `CODEX_MEMORY_ROOT`. Created with `0600` permissions.
- **System Status**: Reported in `WebControl.system_status()` under `features.approval_relay`.

When disabled, zero DB files are created, no background consumer threads/tasks run, and existing endpoints remain unchanged.

---

## 2. Architecture & Data Flow

```text
+-------------------+       +--------------------+       +--------------------+
|    Web Console    |       |    Telegram Bot    |       |     Feishu Bot     |
| (web_console.py)  |       |      (bot.py)      |       |  (feishu_bot.py)   |
+---------+---------+       +---------+----------+       +---------+----------+
          |                           |                            |
          |  publish / decide / claim |  publish / decide / claim  |
          +-------------------+-------+----------------------------+
                              |
                              v
                  +-----------------------+
                  |    Shared SQLite DB   |
                  |  (approval_relay.db)  |
                  |     WAL Mode, 0600    |
                  +-----------------------+
```

### Tables
1. **`relay_approvals`**:
   - `token` (PK): Confirmation token matching local `PendingToolAction`.
   - `origin_channel`, `origin_pid`, `origin_instance`: Identity of the owning process.
   - `tool_name`, `summary`, `arg_preview` (redacted via `redact_text`, capped to 2,000 chars), `danger`, `source` (`chat` | `routine` | `webhook`).
   - `created_at`, `expires_at`, `status` (`pending` | `approved` | `rejected` | `expired` | `cancelled` | `done` | `failed`).
   - `decided_via`, `decided_by` (redacted), `decided_at`, `claimed_at`, `result_preview`.
2. **`relay_notifications`**:
   - `token`, `channel`, `target`, `external_id`, `created_at`.
   - Tracks sent outbound notifications to edit them once a decision is made.

---

## 3. Decision Lifecycle

1. **Publish**:
   - When a dangerous tool requires confirmation (in Web Chat, routines, Telegram, or Feishu), `approval_relay.publish` records a pending row in SQLite.
   - Outbound notifications are dispatched to all configured relay channels **except the origin channel** (the origin chat already has buttons).
2. **First-Decision-Wins (Compare-And-Set)**:
   - When an operator clicks **Approve** or **Reject** on Web, Telegram (`relay:approve:<token>`), or Feishu (`relay_approve` card button):
   - `approval_relay.decide` executes an atomic compare-and-set query:
     ```sql
     UPDATE relay_approvals
     SET status = ?, decided_via = ?, decided_by = ?, decided_at = ?
     WHERE token = ? AND status = 'pending' AND expires_at > ?
     ```
   - Exactly one surface wins (`won`). Subsequent attempts from other surfaces receive `already_decided` or `expired`.
3. **Notification Update**:
   - All recorded messages across all channels are updated via their platform API (e.g. `editMessageText` or Feishu card patch), replacing interactive buttons with the decision outcome (e.g., `✅ 已批准（Web）`).
4. **Owner Execution**:
   - The origin process runs a background `RelayConsumer` polling every ~1 s.
   - It claims decisions matching its local memory store via atomic `UPDATE ... RETURNING *`.
   - The owning process executes the confirmed action locally through existing audit and dispatch paths (`decide_tool_approval` or `execute_confirmed`), prefixing results sent back to the user with `✅ 已在 <Surface> 批准`.
   - Finally, `approval_relay.mark_local` records completion status (`done` or `cancelled`).
5. **Local resolution gate**:
   - The origin chat keeps its own buttons / text YES. Before `execute_confirmed` or `cancel_pending` act, `approval_relay.claim_local` compare-and-sets the shared row (`decided_via='origin'`). If another surface already decided differently (e.g. rejected on Web), the local action is dropped and the operator is told "该审批已在其他端处理（已拒绝），本次操作未执行。". A row that already carries the same decision (the consumer executing a remote approval) passes.
   - `mark_local` only transitions rows that are still open (`pending`/`approved`/`rejected`), so each notification is updated to its outcome exactly once.

---

## 4. Web Approval Inbox Integration

- **`GET /api/approval-inbox`**:
  - When relay is enabled, foreign relay rows (`origin_instance != current_instance` and token not in local pending) are listed as items with `kind="relay"`, `editable=false`, `draft=null`.
  - `counts` includes per-channel tallies (`telegram`, `feishu`).
- **`POST /api/approval-inbox/<id>/approve|reject`**:
  - For relay items, calls `approval_relay.decide(via="web", decided_by="web")` and returns 200 with `status: "accepted"|"rejected"|"already_decided"|"expired"` and `result: ""`.
  - Submitting an edited `draft` for a relay item returns `400 Bad Request`.
- **Web UI**:
  - Displays source badge (`Telegram` or `飞书`).
  - Displays notice: `ℹ️ 跨端中继审批：将在原会话中执行`.
  - Provides Approve and Reject buttons (no Edit option).

---

## 5. Security Model

- **Authentication & Authorization**:
  - **Web Console**: Requires valid Bearer token (`CONVEYOR_WEB_TOKEN`).
  - **Telegram Bot**: Requires authorized operator ID (`TELEGRAM_ALLOWED_USER_ID`). Unauthorized presses trigger an audit event and show an unauthorized alert.
  - **Feishu Bot**: Requires authorized operator Open ID (`LARK_ALLOWED_OPEN_ID`).
- **Redaction**:
  - Raw credentials, API keys (`sk-...`, `ghp_...`), and secrets are screened using `redact_text` prior to storing in the SQLite database, pushing into notifications, logging, or exposing via APIs.
- **Local Database Isolation**:
  - The SQLite database file is created with `0600` permissions and stored within the state directory (`CODEX_MEMORY_ROOT`). Only the local system user running Conveyor can read or write it.
- **Owner-Side Recheck**:
  - Execution only occurs if the origin process still holds the valid, unexpired pending action in memory.

---

## 6. Limitations & Operational Notes

- **Interactive Buttons Only**: Relay notifications on Telegram and Feishu provide inline interactive buttons. Text replies (`/confirm`) are not supported for relay notifications (document-only button flow).
- **No Cross-Channel Editing**: Actions originating from Telegram or Feishu cannot be modified from the Web Console draft editor (`draft` returns HTTP 400).
- **Single-Host Only**: Conveyor processes must reside on the same host (or share a filesystem supporting POSIX file locks) to access the SQLite relay database.
- **Execution Latency**: Because the origin process claims decisions via polling, execution starts within ~1 second after approval.
- **Feishu Push Requirements**: Feishu push notifications require `LARK_ALLOWED_OPEN_ID` configured to identify the operator recipient. If unset, Feishu outbound notifications are skipped, though button approvals on existing cards still function.
- **Notification I/O off the event loop**: real Telegram/Feishu notifier HTTP calls run on one background worker thread; bot callbacks run `decide` via `asyncio.to_thread`. (A test notifier factory makes dispatch synchronous.)
- **SQLite ≥ 3.35** is required (`UPDATE ... RETURNING`).
- **Click-testing without bots**: `scripts/approval_relay_fake_bot.py` simulates a Telegram process fully offline (stub tool runner, fake notifier). See the PR description for steps.
