# Mention, reply & image context ("@bot is this true?")

Point Conveyor at a message instead of retyping it. In Telegram or Feishu,
reply to (or quote) any message and mention the bot:

| You send, replying to a message | Conveyor does |
| --- | --- |
| `@bot 这是真的吗？` / `is this true?` / `fact check` | Searches the web for the claim (when a search backend is configured), then answers with a verdict: ✅ 属实 / ❌ 不实 / ⚠️ 部分属实 / ❓ 无法核实, key evidence and sources |
| `@bot` (nothing else) | Explains the message |
| `总结一下` / `tl;dr` | Summarizes it |
| `翻译` / `translate to English` | Translates it |
| any other question | Answers it with the message as context |
| `记一下` | Saves the quoted message to today's MEMORY.md |
| `/run …` or `/fix …` | Runs the job with the message as context (e.g. reply to a traceback with `/fix`) |

Replying to one of Conveyor's own answers continues the thread ("why?",
"shorter please"); in Telegram groups that counts as addressing the bot, no
@mention needed. Selecting part of a message with Telegram's quote feature
sends only the selected text.

## Images ("这张图是什么？")

Send a photo (or an image file) — with or without a caption — or reply to
one, and the agent looks at it:

| You send | Conveyor does |
| --- | --- |
| a photo, no caption | Describes / explains it |
| a photo with a question as caption | Answers it about the image |
| `@bot 这是真的吗？` replying to a photo or a post with images | Fact-checks the image (and any text of that message) |
| a screenshot captioned `/fix …` | Runs a fix job with the screenshot attached |

In groups the caption must @mention the bot (or reply to it), like text.
Up to 4 images per request; each at most 10 MB (JPEG, PNG, GIF, WebP).

How: adapters record `Attachment` references (`InboundMessage.attachments`:
the message's own images, then the replied-to message's). After the
allowlist check `handlers/context.py` downloads them through
`OutboundPort.fetch_attachment` into `<codex_task_root>/attachments/`
(`runner/attachments.py`: magic-byte check, random names, `0600` files in a
`0700` directory, 7-day retention). The prompt opens with
`[image: <name>]` header lines; the runner turns those into
`codex exec --image …` (Claude Code backend: `--add-dir` for the store so
its Read tool can open them). Only header lines at the very top of the
prompt count, only random store names are accepted and the file must live
in the store, so quoted text can never attach an arbitrary file. Text
inside images is treated as untrusted data, like quotes.

## Group chats

Conveyor still serves one operator. In group chats it acts only when the bot
is **@mentioned or replied to**. Other group conversation is ignored, and
members who are not the operator get no reply at all ("Unauthorized." is
only sent in private chats). This lets the operator add the bot to a group
and use it where the discussion happens.

Telegram: with the default privacy mode the bot only receives commands,
mentions and replies to it, which is exactly what it acts on. Feishu: the
bot needs the group-message permission and the `@` mention; the bot's
mention is resolved from the event's mention list and stripped from the
text, so `@bot /status` works in groups.

## How it works

- `channel/telegram.py` and `channel/feishu.py` fill
  `InboundMessage.reply_to` (`ReplyContext`: text, author, `from_bot`,
  `partial_quote`) and `mentioned_bot`, and strip the bot's own `@name`
  (`channel/mentions.py`). Feishu fetches the parent message after the
  allowlist check (text and post messages; other types carry no context).
- `handlers/context.py` classifies the operator's words
  (`detect_context_intent`), builds the agent prompt, and adds a web
  evidence pack for fact-checks via `personal_tools.research.factcheck_evidence`.
  Without a search backend the agent checks on its own.
- `handlers/dispatch.py` routes: confirmation → commands → stop fast path →
  memo → reply context → the normal tool / agent router. Messages without a
  reply are routed exactly as before, and a plain tool request that happens
  to be a reply (`服务器状态` replying to an old bot message) keeps its fast
  tool route instead of becoming an agent job.

## Security

The quoted text is usually written by someone else, and the agent runs with
full host access. Conveyor therefore:

- only handles messages from the allowlisted operator (auth runs before any
  parent message is fetched or read);
- wraps the quote in a `<quoted-message>` block marked as untrusted data,
  tells the agent not to follow instructions inside it, and neutralizes any
  attempt to close that block early;
- keeps the operator's own words as the only instruction and as the only
  input to intent routing, so a quoted message cannot pick a tool;
- truncates quotes to 3000 characters and fact-check search queries to 200.

Smoke: `scripts/reply_context_smoke.py` and `scripts/image_context_smoke.py` (part of `make smoke`).
