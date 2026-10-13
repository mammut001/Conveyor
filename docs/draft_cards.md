# Editable outbound draft cards (default off)

## Enablement

Set `CONVEYOR_DRAFT_CARDS=1` and `CONVEYOR_APPROVAL_INBOX_ENABLED=1`. The feature is **off by default**; turning it off preserves existing approval behavior for new actions. Configure `CONVEYOR_DRAFT_TTL_SECONDS` (default 86400, range 300 seconds–7 days), and optionally `CONVEYOR_DRAFT_CARDS_WEB_URL=https://<your-private-console>` to add an **Edit in web** link to Telegram/Feishu approval relays. Never expose an unauthenticated web console to the internet.

Set `CONVEYOR_OUTBOUND_ALLOWLIST=example.com,operator@partner.net` to restrict **all** To/Cc/Bcc SMTP recipients to exact email addresses and/or whole domains (no subdomain wildcard). A populated allowlist fails closed on outbound tools with unknown recipients, including GitHub comments and MCP sends without recipient fields. A blank allowlist imposes no recipient restriction.

## Supported outbound tools

* `email.send` uses its existing `to | subject | body` tool syntax. When draft cards are enabled, the draft also supports To, Cc, Bcc, subject and body editing. Advanced tool calls can supply a JSON object with the same fields. The SMTP implementation sends Cc/Bcc as recipients in the envelope, but deliberately omits a Bcc header.
* `github.comment` uses existing `issue/PR number | body` syntax. The web draft exposes repository, issue/PR number and comment body. GitHub issue/PR comments both use the Issues Comments endpoint.
* MCP tools explicitly annotated `outbound: true` (or tagged with `outbound`) and classified WRITE/DESTRUCTIVE get an editable JSON payload draft. Untagged tools retain existing behavior. The original tool's MCP allowlist and danger-level gating are still required. With a recipient allowlist set, all addressable recipients must be known and permitted.
* Telegram and Feishu transport messages are **approval relays**, not separately registered agent send tools. They show read-only previews with Approve/Deny plus an optional configured Web link. A Telegram/Feishu Approve sends only the original unedited hash-bound draft. Editing is Web-only.

Only the authenticated Web Approval Inbox exposes editable fields. The `Send` action posts the entire edited draft plus the immutable original preview SHA-256. Server-side validation rejects stale/tampered hashes, expired cards, header CR/LF injection, invalid addresses, invalid repo/issue targets, unsafe lengths and allowlist violations. The Web UI does not have access to SMTP/GitHub/MCP secrets.

## One-shot send contract

Draft cards live in a separate SQLite database under `codex_memory_root/outbound_drafts.db`, secured with file mode 0600. The original pending approval's token is reused, and its TTL is extended to the configured draft TTL. Before touching an external endpoint, the server commits a **single atomic claim** from `pending` to `sending`. Only one claimant can win even if Web Send, Telegram Approve, Feishu Approve or duplicate API requests race.

After a successful operation, the status is `sent`. For any exception, timeout, ambiguous response or network failure after the claim, status is `needs_reconciliation`: **never retry automatically**. In-flight attempts and uncertain outcomes appear as read-only reconciliation alerts in the Approval Inbox. Check the destination manually before creating a new draft. Terminal records keep only token, status, original hash, sent hash and redacted result; the plaintext payload is scrubbed from the row. Database backups and filesystem snapshots can still retain previous versions and must be protected separately.

`Deny` is terminal and does not transmit. A rejected or expired draft cannot send. If the feature is enabled and a draft record is missing or unavailable, outbound confirmation is blocked, not downgraded to legacy approval. Web legacy `/api/approvals/{id}/approve` and `/api/approval-inbox/{id}/approve` cannot bypass a registered draft card. The agent only receives an operation result, never the revised human-authored content.

The Web editor shows **Save as draft only** only when a sending transport implements a verified save-draft operation. Neither the current SMTP sender nor GitHub's comment endpoint supports server-side drafts, so this option is intentionally unavailable for them; pressing Send or Deny remains explicit.

## Verification

Run `python -m unittest discover -s tests -p 'test_outbound_draft_cards.py' -v` and `python -m unittest discover -s tests -v` plus `npm --prefix web run build`. The test suite uses a fake SMTP endpoint and mocked GitHub requests, and checks exact edits, Bcc envelope, replay and one-shot behavior, expiration, malformed headers, recipient allowlist and default-off behavior.
