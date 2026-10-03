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
# default true — one store shared by web, Telegram and Feishu
CONVEYOR_LONG_TERM_MEMORY_SHARED=true
# default false — group chats get no durable memory
CONVEYOR_LONG_TERM_MEMORY_GROUPS=false
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

Secrets, API keys, and tokens are refused (`redact_text`), as are plain-language
credentials: a credential word (密码 / password / PIN / token / API key …) followed by a
password-like value (letters + digits, or 4+ digits). They are not
stored, including not stored in redacted form, and they are not copied into
the pending approval.

## What a new chat sees

Facts are not keyed by chat id. With `CONVEYOR_LONG_TERM_MEMORY_SHARED=true`
(default) every channel reads and writes one store (operator key `owner`), so a
fact remembered on Telegram is seen on Feishu and in the Web Console. This is
safe because Conveyor is single-operator: Telegram accepts one user id, Feishu
one open_id, the Web Console one token. Each row records `source_channel` for
provenance. Set the flag to `false` to key facts by channel-specific operator
id instead.

## Group chats get no memory

Other people read the replies in a group, so by default a group chat neither
sees nor changes durable memory:

- nothing is injected into the prompt (no profile, no log, no "memory is on"
  instructions);
- `memory.*` tools are not offered to the model, and a call to one anyway is
  answered `unknown tool`;
- `记住 …` / `忘掉 …` and any direct `memory.*` invocation reply
  "长期记忆只在私聊和 Web 控制台可用…" and create no approval.

Chat type comes from the channel event: Feishu `chat_type` (`p2p` allowed,
`group` refused, anything else treated as unknown and refused), Telegram
`chat.type` (`private` allowed; `group`, `supergroup`, `channel` refused). The
authenticated Web Console always has memory. `CONVEYOR_LONG_TERM_MEMORY_GROUPS=true`
opts groups back in.

Each prompt gets a bounded slice, not the whole store:

- **Profile** — the first `8` explicit facts (the always-on set). Later
  remembers do not push these out. Char budget about 1200.
- **Recent log** — facts after the profile is full, newest `4`, char budget
  about 600.

The whole block is capped (about 2000 characters). Older log rows stay in
SQLite until forgotten or until the log itself is past 200 rows (oldest log
rows are pruned; profile rows are not). Use `memory.search` for anything
outside the slice.

- **Relevant older log** — up to 3 older log rows that match the current
  message (about 400 chars), so a fact pushed out of the recent slice can still
  come back when asked about.

The block is labeled as data, not instructions.

## Search

`memory.search`, the Web page and relevance injection share one matcher.
Chinese text is split into overlapping 2-character terms with filler words
(什么 / 我的 / 一下 …) dropped, so `我的猫叫什么` finds `我的猫叫团子`.
Latin words are matched case-insensitively with stopwords dropped. Rows are
ranked by matched terms; a whole-query substring match ranks first.

## Web Console

A **Memory** tab sits next to Tasks / Chat / Inbox when the flag is on
(`/api/system/status` reports `features.long_term_memory`). `/#memory` or
`/memory` opens it directly. The view lists, searches, adds and deletes facts. Delete needs an
explicit second click (`Confirm delete`). Adding from the page runs the same
secret / credential filter; there is no model in the loop, so typing the fact
and pressing Add is the confirmation.

Authenticated API (same bearer token as the rest of `/api/*`, 401 otherwise):

- `GET /api/memory?q=&kind=profile|log&limit=` → `{items, counts, shared, profile_cap}`
- `POST /api/memory {"text": "..."}` → 201, or 400 if refused (secret, empty, too long)
- `DELETE /api/memory/<id>` → 200, 404 if missing

All return 409 while `CONVEYOR_LONG_TERM_MEMORY` is off.
