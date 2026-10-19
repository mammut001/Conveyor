# Web chat interaction regression checks

The initial validation below was recorded on the older checkout. For current-main
compatibility and remaining platform gates, see [integration evidence](experience-current-main-integration.md).

Verified locally on 2026-10-18. These checks cover the real `ChatPanel` in
Chromium, using a controlled HTTP/SSE endpoint. They do not measure a live
provider's latency or prove that a production deployment has been updated.

## User flow and changes

The flow under test is: type a message → authenticated `/api/chat` POST →
SSE session/progress/answer → rendered conversation and editable composer.

Previously, Enter during Chinese IME composition could submit a message;
the composer was disabled for the entire reply; every streaming update forced
a scroll to the end; interrupted responses disappeared; and a slow initial
history response could replace a newly submitted turn.

The composer now protects IME confirmation and permits drafting the next message
while a request is running. It shows immediate waiting feedback. Reading history
suspends automatic scrolling, with an explicit button to return to the latest
message. On interruption, partial text remains visibly marked as incomplete,
and an unanswered failed request returns to the composer if no newer draft exists.
No failed request is automatically resent. Stale history responses are ignored,
and switching to another console view keeps the chat and its in-flight reply
alive. Returning to Chat shows the completed reply and any next-message draft.
Actual component disposal still aborts its fetch.

Drafts use per-agent keys in tab-scoped `sessionStorage`, restore after a browser
reload, and are cleared when sent or when a free-chat conversation is explicitly
reset with New. Storage failures do not block typing. Background generation is
retained across console view changes; it is not resumed after a full browser
reload or an agent change.

When the chat tier requests execution or is unavailable, its answer now includes
an explicit task suggestion. “准备执行任务” opens the task composer with the original
request and durable session. Preparing does not queue a task; the operator can
edit and send it. The authenticated task endpoint accepts `execution_only: true`
for this explicit handoff and submits directly through the existing Codex job
queue instead of calling the chat model again. Conversations with queued jobs
also appear in the task session list. Suggestions are restored from history;
session identity comes from the transcript session rather than redacted metadata.

## Reproduce

Run `npm ci` in `web/`, then `npx playwright install chromium` there once to
install the browser. Run `npm --prefix web run test:chat` from the repository
root. The Web CI job installs Chromium and runs this same command on changes.
If Playwright is supplied by an external runtime, set `PLAYWRIGHT_MODULE_PATH`
to its module entry point. The script starts Vite on a temporary loopback port, exercises the actual component,
and closes its browser and server when finished. It makes no provider calls.

The browser checks cover:

- Chinese composition confirmation, including the legacy key-code fallback.
- Shift+Enter newline insertion and normal Enter submission.
- Immediate waiting feedback and editing the next draft during a reply.
- Staying in history while text streams, then returning to the latest message.
- Preserving an interrupted answer and an already-written next draft.
- Restoring an unanswered failed request without automatically resubmitting it.
- Rendering a completed answer once, with no lingering error.
- A slow history response arriving after a new message has been sent.
- A 390-pixel viewport with a visible composer and no horizontal overflow.
- Full `App` navigation from a chat suggestion to the task composer, no execution
  before Send, the original prompt/session in the POST, the promoted conversation
  in the task list, and the restored suggestion after returning to Chat.
- The task composer also protects Chinese IME confirmation.
- Draft restoration across browser reloads and isolation between agent drafts.
- Switching from Chat to Tasks during generation keeps the HTTP stream alive;
  the answer completes in the background and the next draft remains intact.
- New clears the current free-chat draft without clearing other agent drafts.
- Full App mobile layout with the mobile feature enabled.
- No uncaught browser runtime errors.

Additional local validation passed:

- Full unittest discovery: 689 tests, 2 skipped, all remaining tests passed.
- Desktop Computer Use smoke: 47/47 passed.

- `npm --prefix web run build` (type check and production bundle).
- `CONVEYOR_ENV_FILE=.env.test.example .venv/bin/python -m unittest discover -s tests -p 'test_web_chat.py'`: 15 tests, including escalation and unavailable handoffs.
- `CONVEYOR_ENV_FILE=.env.test.example .venv/bin/python -m unittest discover -s tests -p 'test_mobile_web.py'`: 13 tests.

## Remaining evidence needed

The operator's primary usage channel and specific dissatisfaction have not yet
been confirmed. This improves verified Web interaction problems; it does not
establish that the broader experience goal has been met. Production response
latency, answer quality, and desktop task success require their own evidence.

## Structured answer rendering

The shared reply renderer now uses `react-markdown` with `remark-gfm`, loaded
only when there is reply text to render. Headings, ordered and nested lists,
quotes, links, tables, and open streaming code fences render as structured
content. Existing turns are memoized while the next reply streams. Tables and
long code lines scroll within the reply, including at a 390-pixel viewport.
Code copy preserves indentation and blank lines and displays a failure state
if the browser denies clipboard access.

The browser regression checks cover these formats, links containing parentheses,
HTML escaping and blocked script URLs, image references staying explicit links,
clipboard success and denial, local table/code overflow, and a deliberately
failed markdown module request falling back to readable text. Reply size changes
and viewport reflow keep the latest answer in view while respecting readers who
have scrolled up into history.

Production build after this change: main JavaScript about 351 KB (100 KB gzip),
plus the deferred markdown chunk about 157 KB (47 KB gzip). Runtime on the VPS
continues serving static assets without requiring Node.js.

## Task progress and transcript recovery

The task view now merges the initial replay with events received over SSE instead
of replacing the live stream when the replay arrives late. Events stay unique
and ordered by durable sequence, and reconnect cursors never move backwards.
Interrupted connections display a reconnect notice while retaining received
records. Each abandoned connection is aborted before its replacement starts.

Transcript loads are scoped to the selected session and generation, so delayed
responses cannot overwrite another conversation. Polling shares an outstanding
request instead of starting overlapping reads; a hung read times out after 15
seconds so later reads can recover. A failed read retains existing content and
provides an explicit retry action. Failed, cancelled, and interrupted task notices
remain visible even when the conversation already has history. Terminal tasks
no longer show the new-session welcome screen.

Controlled browser scenarios cover a delayed replay arriving after live events,
ordered deduplication after reconnect, a monotonic reconnect cursor, each task
failure/cancellation/interruption notice with prior history, a delayed transcript
arriving after session selection changes, a temporary transcript outage followed
by retry, overlapping polling prevention, and recovery from a hung transcript
request. These are UI/transport regressions with controlled endpoints, not
measurements of a live provider or a deployed production service.
