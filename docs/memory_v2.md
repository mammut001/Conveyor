# Structured memory v2 (opt-in)

Status: experimental, default **off**. V2 extends Conveyor's existing durable
memory implementation, rather than replacing chat history or the daily journal.
It uses new memory_v2_entries and memory_v2_fts tables inside
<codex_memory_root>/long_term_memory.db. The legacy memories table remains
untouched. There is no automatic migration and no changes to the historical
chat_memory.db, session JSONL or MEMORY.md while the flag is disabled.

## Flags

Set CONVEYOR_MEMORY_V2=1 to use v2 instead of the legacy durable-memory backend.
Default is 0. CONVEYOR_LONG_TERM_MEMORY continues to control **only** the
original backend when v2 is off. When v2 is on it takes precedence over legacy,
and memory tools become exposed in the chat tier if CONVEYOR_CHAT_TOOLS=true.
CONVEYOR_MEMORY_V2_RECALL_K=5 selects up to 5 relevant fact entries per turn
(0-20). CONVEYOR_MEMORY_V2_TOKEN_CAP=1000 sets an approximate 2-character per
token envelope (bounded 128-4000 tokens). No model data is used to silently
create memories. CONVEYOR_LONG_TERM_MEMORY_SHARED and
CONVEYOR_LONG_TERM_MEMORY_GROUPS keep their existing semantics; group chats
cannot read/write memory unless explicitly opted in.

## The three tiers

- profile: explicit operator facts only, **2048 UTF-8 bytes** per owner in
  storage. The whole profile is eligible for every chat turn. On overflow,
  writing fails instead of discarding/demoting facts.
- facts: individual statements with created_at, updated_at, source,
  source_channel, agent_scope, optional expires_at (Unix timestamp), and a
  non-reused ID. Search is scoped by owner and excludes expired records.
- log: up to 4000 characters per entry, ordered by date. The existing
  scripts/compress_day.py appends a dated snapshot of v2 log entries to the
  same JOURNAL archive as MEMORY.md. Rows remain in SQLite until explicitly
  forgotten. Importing the journal does not auto-promote its entries.

The default operator owner is shared across Web, Telegram and Feishu when
CONVEYOR_LONG_TERM_MEMORY_SHARED=true. Each non-default agent uses its own
agent:<id> owner key, including for its memory API calls and recall. No
cross-agent fallback or global memory search exists. Browser pages, tool
results, web data and historical transcripts are not trustworthy operator
profile instructions. Any consciously imported records from these sources
are tagged source=untrusted and never auto-promoted to profile. The chat
tools require an explicit operator request; there is no background harvesting.

## Chat and recall

Tools reuse the existing PERSONAL_TOOL_REGISTRY and approval cards:

- memory.remember accepts a JSON object with text and tier (profile, facts,
  log). Defaults to facts for legacy plain-text invocations. This is WRITE and
  **requires human approval**; no WRITE_SAFE special case is granted.
  Model-rephrased requests (or text derived from pages, chat tools and other
  untrusted content) are saved with source=untrusted and **cannot** become
  profile entries through the chat tool. Use the Web Console Memory page to
  edit/create profile facts, or issue an explicit verbatim `remember ...`
  command whose text exactly matches the saved fact.
- memory.search(query) and memory.list are READ.
- memory.forget(#id) is WRITE with approval. At approval creation the action
  is bound to `#id@sha256(text)` and checked again upon execution; stale
  approvals cannot delete edited/replaced records. IDs use SQLite
  `INTEGER PRIMARY KEY AUTOINCREMENT` (existing v2 tables migrate in place,
  preserving IDs). Fuzzy/ambiguous deletion is not supported in v2.
  `PRAGMA secure_delete=ON` is used on every v2 connection; after deletion
  (or replacing text), the FTS5 index is optimized and WAL checkpointed with
  TRUNCATE to scrub FTS shadow content and on-disk WAL copies. Deletion
  reports an error if the WAL checkpoint cannot finish, rather than
  promising disk sanitization. Backups and separately archived JOURNAL /
  MEMORY.md copies are outside the SQLite purge scope.

A chat turn receives the bounded profile, up to k query-matching facts, and
the newest two daily log entries. Search uses SQLite FTS5 with a LIKE fallback
(CJK substring matching uses LIKE as well). All recalled rows are wrapped in
<memory-v2 role="untrusted-data">, escaped as HTML text and explicitly
described as **data, never instructions**. They cannot supply tool permissions.
Secret and credential screening runs before writing *or creating an approval*;
no raw credentials are saved as memory.

Telegram/Feishu commands: /memory show, /memory search <query> and
/memory forget <id> work when v2 is on. Forget requests approval. The original
/memory [date] [category], /memo, /journal and /chat_clear are unchanged.
Group chat safeguards are reused. The interface remains a shared command
dispatcher; there is no second Telegram command implementation.

## Web Console

The existing Memory page is reused (including per-agent selection). API calls
are authenticated through the same bearer-token guard as other /api routes:

- GET /api/memory?q=&kind=profile|facts|log&agent=<agent-id> lists and searches.
- POST /api/memory with {"text":"...","tier":"facts","agent":"optional-id"}
  creates an explicitly operator-entered record. Tier defaults to facts in v2.
- PUT /api/memory/<id>?agent=<agent-id> with {"text":"..."} edits just the
  record text, keeping original tier, scope and source; invalid or oversized
  changes fail. The page shows an Edit action when v2 is enabled.
- DELETE /api/memory/<id>?agent=<agent-id> deletes a scoped record; the
  page uses a second confirmation click.

Requests for unknown or archived agents return 404. When both memory flags
are off, existing API 409 behavior is retained. When only legacy memory is
enabled, existing legacy API/search/schema behavior is retained; PUT needs v2.

## One-time import (never automatic)

Review imported material before use:

    CONVEYOR_MEMORY_V2=1 python -m personal_tools.memory_v2_import --dry-run
    CONVEYOR_MEMORY_V2=1 python -m personal_tools.memory_v2_import

The importer reads the old long_term_memory.db memories table, historical
JOURNAL/*.md and active daily worktree MEMORY.md files. Old profile entries
retain the profile tier if they fit. Old log facts become v2 facts, not profile.
Journal lines become untrusted log entries. Imports are idempotent using
unique source keys. Re-running reports already_present instead of duplicating.
The --dry-run option does not modify the DB. Inputs failing credential
screening, entry size validation or profile budget are skipped.

To import historical chat_memory.db and session/*.jsonl as well, explicitly
add --include-conversations. **These are never promoted to profile/facts.**
They are stored under isolated import:chat:* / import:session:* owners, not
available to the operator/agents by default. They remain available in their
original source stores even if not imported, and may contain private or
low-trust text. No operation removes or modifies original sources.

## Verification

Run python -m unittest tests.test_memory_v2 -v, then the usual
python -m unittest discover -s tests -v, make smoke, and web build.
Important assertions: default flag off unchanged; FTS/LIKE search and
post-delete absence; profile UTF-8 byte cap; agent cross-scope refusal;
untrusted text envelope escaping; importer second run imports 0;
browser/API edit cannot change another agent's record. Deploy v2 disabled
until explicitly opted in. Rollback by disabling CONVEYOR_MEMORY_V2;
the original memories table and journal still exist.
