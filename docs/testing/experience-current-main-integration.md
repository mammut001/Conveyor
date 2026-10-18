# Experience improvements integrated with current main

Base commit: `8617183efd6f510fb68411e2faff564bae2acf35`.
Integration branch: `codex/experience-improvements`.

The original checkout was on the older `fix/linux-computer-use-reliability`
branch. Earlier local test results apply to that older checkout, not current
production. The improvements were ported into an isolated managed worktree based
on current main; the original checkout and its uncommitted changes remain intact.

## Production baseline

A read-only SSH check on 2026-10-18 confirmed that production HEAD matches the
base above. Web, Telegram, and Feishu services were active with `NRestarts=0`.
All three running processes had chat auto mode, model configuration, a chat key,
and Agents enabled. Only booleans and service/version metadata were reported;
credentials were not copied or printed. No production configuration, queues,
databases, service state, or code were changed.

## Compatibility retained

- Preserve current main's Agent workspace, Inbox, and delegated Tasks screens.
  Switching away from its Chat keeps pending replies and drafts alive, including
  while the feature configuration is loading.
- Preserve voice submission and transcription events. Sending a voice message
  retains an already typed draft.
- Preserve multi-turn session run history, cumulative change review, chain
  approvals, Workflows, browser management, Secrets, and PWA navigation.
- Preserve attached model finish-reason/usage metadata for bounded delegation
  recovery, and strip that local metadata from subsequent provider requests.
- Preserve the current named-subagent dispatch and approval policy when adding
  tool progress feedback.
- Keep the existing browser E2E command alongside the new chat interaction checks.
  Align Playwright versions and disable silently reusing an unrelated server;
  `CONVEYOR_E2E_PORT` can select a free local test port.

## Evidence so far

- Typecheck and production build passed on this base.
- The 11 existing Web E2E cases passed on an owned server at an isolated port.
- Two additional Agent E2E cases passed: pending reply/next draft survive Inbox
  navigation with a narrow viewport; voice submission preserves a typed draft
  and renders the transcription and answer.
- Targeted backend checks: 6 chat connection lifecycle tests, 20 chat-tool tests
  including model metadata, and 22 current delegation/recovery tests passed.
- Full macOS unittest discovery ran 1,279 tests: 100 skipped, 9 errors and 1
  failure. All 10 failing cases were independently reproduced against a clean
  archive of the unchanged base using the same Python environment. They require
  Linux browser ownership/process identity (`/proc`); the failed timeout case
  receives `login_unavailable` before it can reach its intended login timeout.
- Full smoke execution also stopped at that platform-dependent browser-auth
  group. These gates are not reported as passing, and no assertions or skip
  conditions were weakened.

## Telegram / Feishu focus

The operator identified slow replies, awkward commands, and broken continuity
in Telegram / Feishu as the priority. The shared tool loop now applies one
model-wait budget across rounds instead of allowing every round to wait for a
fresh full timeout. Tool execution is excluded from this budget. Regression
checks verify that timeout cancels the pending model call, and a slow read tool
is not cancelled or charged against model time.

The chat answer path, `/deep`, and `/chat_clear` now use the same Agent-scoped
history key. Previously ordinary replies used scoped history while these command
paths used legacy chat keys. Tests verify that clearing one Agent preserves
another, and that each Agent's last request can be recovered from SQLite for
`/deep`. The combined chat, lifecycle, Agent context/compatibility and Web chat
regression selection passed 60 tests after these changes.

The latest standalone Web chat smoke run passed all 18 check groups. The earlier
hung-read retry failure was a test race: polling could recover and remove the
retry button before Playwright clicked it. The hung-read case now asserts
polling recovery and preserved content; the separate explicit retry check
remains. No application recovery behavior was weakened.

## Remaining verification

Verify real Telegram / Feishu interactions after an authorized deployment. The local
runtime has no chat model configured; that does not imply production chat is
disabled. Overall experience improvement and production acceptance remain
unproven. The integration branch has not been deployed.

## Phone command discoverability

Whole phrases such as “看看任务进度”, “查看帮助”, and “清空当前对话”
now enter the existing authenticated command path before model routing. Quoted
messages and attachments retain their context path. Phrases embedded inside a
longer task do not match. `/help` is a short common-operations guide; the full
inventory remains at `/commands`. Slash arguments now accept tabs and newlines
from mobile keyboards. No new mutation command or confirmation bypass is added.

The current phone regression selection passed 80 tests (conversation controls,
computer dispatch, Telegram navigation, Agent context/compatibility, chat tools,
HTTP lifecycle, and Web chat). The chat-tier routing smoke also passed.

## Isolated Linux verification

The current source snapshot ran against Python 3.10.12 and the production
dependency environment inside bubblewrap. The root filesystem was read-only;
`/opt/conveyor`, `/home`, `/srv`, and `/run` were masked; `/tmp` was private;
network and process namespaces were isolated; environment variables were cleared.
Production code, databases and services were not changed.

Full unittest discovery passed: **1,290 tests, 93 skipped**. Python 3.10's
`mock.patch` traversed the exported `handlers.dispatch` function instead of its
module in several tests; those mocks now target the imported module explicitly.
The approval handoff test now uses the offline synchronous notification factory
so no background notification can reopen its database during temporary-directory
cleanup. The full smoke gate requires Git metadata; an isolated snapshot commit
was created in the private test directory for its version checks. Inventory
smokes retain their existing assertions and now check `/commands`.

A separate read-only probe used the existing configured provider with a fixed
short synthetic prompt and no tools. It received its first text after **1.290s**
and completed after **1.304s** (12 characters). No channel message was sent, no
user conversation was used, and no credentials or response content were printed.
This single basic request does not establish Telegram/Feishu end-to-end latency
or the performance of multi-tool replies.

Final Linux `make smoke` passed all **75 configured smoke scripts**. This
includes the browser ownership checks that could not run on macOS. The isolated
snapshot and its Git metadata were private temporary test state. No production
service restart or code update was performed.
