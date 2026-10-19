# Independent conversations acceptance evidence

The operator explicitly selected independent Telegram/Feishu/Web histories by
default with deliberate continuation of a chosen existing conversation.

## Covered behavior

`tests/test_agent_conversations.py` covers:

- Separate Agent/entry history keys, context files, and durable transcripts.
- The real chat dispatcher omits another entry's history until continuation.
- Explicit continuation shares history while preserving Web/phone identity,
  operator and Agent capability boundaries.
- An in-flight reply writes to its original conversation after selection changes.
- Expired short-term context is rebuilt from the selected transcript; a context
  reset prevents old history from being reintroduced while retaining records.
- Agent switches restore that entry's last selection; local roots stay stable.
- Legacy Web conversations retain their keys/files. The default assistant's own
  legacy phone context stays available without importing mixed channel logs.
- Unknown, archived, foreign-Agent, non-owner, group and forged Web identities
  cannot select or execute a conversation.
- Queue metadata persists the conversation and bounded execution identity.
  Recovery discards unrelated inherited request context without gaining tools.
- Worktree refinement identity remains scoped to the physical entry and topic;
  the Web workbench resolves that chain without transferring another entry's
  task-worktree authority.

`tests/test_web_chat.py` exercises the actual HTTP adapter: bearer authentication,
origin protection, new-topic creation, canonical Web identity, bounded tools,
unknown aliases, SSE delivery, stored history and archived-Agent rejection.

`web/e2e/agent-screens.spec.ts` covers selecting another entry's conversation,
isolated drafts, switching Agents and restoring selection, creating a topic,
sending with its session ID, disabling selection while a reply is pending,
retrying a failed conversation lookup and narrow-screen overflow. The full Web
suite and standalone chat smoke retain the pre-existing interaction checks.

## Test environment

Linux verification uses the same Python 3.10.12 dependency runtime as production
inside bubblewrap. Production source, home directories, worktrees and service
runtime directories are masked; root is read-only and temporary/process/network
namespaces are private. The environment is cleared and tests use placeholder
configuration. The source snapshot has its own ephemeral Git commit for the
smoke version checks. No bot messages, real model calls or production writes are
part of these tests.

A pre-existing approval store test race could reopen a temporary DB through a
background notification during cleanup. Store-only/callback/HTTP fixtures now
use synchronous offline notifier factories; the separate real-notification
isolation test and all existing assertions remain.

A read-only check confirmed the existing named-Agent, canonical-context and
channel-routing flags are enabled in Telegram, Feishu and Web production
processes. No flags were changed. Deployment still requires the operator's
separate approval and validation of the final merged revision.

## Final results

Code candidate: `f991bdd` on `codex/experience-improvements`.

- Full isolated Linux unittest discovery: **1,308 tests**, **93 skipped**, passed.
- Full isolated Linux `make smoke`: **75 scripts**, passed.
- Typecheck and production Web build: passed.
- Full Web Playwright suite: **16 cases**, passed.
- Standalone Web chat interaction smoke: **18 groups**, passed.

The latest backend snapshot includes the legacy-entry continuity path and the
recovered capability-bound request identity. The final Web build and browser
checks also include the narrow-screen provenance/time wrapping adjustment.
No production deployment, restart, queue mutation, or channel message was made.

Mobile picker preview is a local fixture screenshot, not a live production chat.

## UI follow-up verification

UI code candidate: `150ccd0`. Backend code is unchanged from `f991bdd`.
The final UI revision passed typecheck/production build, all 16 Playwright
cases and all 18 standalone chat smoke groups. The conversation browser case
also verifies initial form focus, Escape and cancel without changing the
selected session or its draft, retained topic title, and Enter submission.
These targeted assertions passed against the final production build.

The 390×844 fixture preview confirms compact Agent cards, a separate conversation
selector and global navigation without a clipped heading. This remains local
acceptance; no publication or real channel acceptance has occurred.
