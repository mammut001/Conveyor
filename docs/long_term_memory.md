# Durable long-term memory

This is not today's `MEMORY.md` journal and not the per-chat short-term
window (`chat_memory.db`, last N turns). Those stay as they are.

Durable memory is a separate SQLite file,
`<codex_memory_root>/long_term_memory.db`. The operator explicitly asks to
remember or forget one fact. A new conversation (new chat id, empty
short-term history) still receives those facts.

## Flag

```dotenv
# default false — tools hidden, prompts unchanged
CONVEYOR_LONG_TERM_MEMORY=false
```

Writes also need the chat tool bridge (`CONVEYOR_CHAT_TOOLS=true`), same as
other chat-tier tools. With the memory flag off, `memory.*` tools are not
offered and nothing is injected. With it on but chat tools off, facts already
stored are still injected, but the model cannot add or delete them.

## What the operator can do

- **Remember** one self-contained sentence: `记住 <事实>`, `remember <sentence>`,
  or a `memory.remember` tool call. The sentence is not saved until the
  operator confirms, using the same confirmation flow as other write tools.
- **Forget** one fact: `忘掉 <原句或 #id>`, `forget <#id>`, or `memory.forget`.
  Forget **deletes** the row. A later new chat does not see it.
- **List / search** (`memory.list`, `memory.search`) are read-only and run
  without confirmation, including facts that were not injected.

`/memo`, `记一下`, and the daily `MEMORY.md` file are unchanged. `/chat_clear`
clears short-term history only.

Secrets, API keys, and tokens are refused (`redact_text`). They are not
stored, including not stored in redacted form, and they are not copied into
the pending approval.

## What a new chat sees

Facts are keyed by operator, not by chat id, so web, Telegram, and Feishu
chats for the same operator share them.

Each prompt gets a bounded slice, not the whole store:

- **Profile** — the first `8` explicit facts (the always-on set). Later
  remembers do not push these out. Char budget about 1200.
- **Recent log** — facts after the profile is full, newest `4`, char budget
  about 600.

The whole block is capped (about 2000 characters). Older log rows stay in
SQLite until forgotten or until the log itself is past 200 rows (oldest log
rows are pruned; profile rows are not). Use `memory.search` for anything
outside the slice.

The block is labeled as data, not instructions.
