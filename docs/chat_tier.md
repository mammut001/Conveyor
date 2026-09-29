# Chat tier ("intent mode")

By default every free-text message starts a Codex job: a detached worktree,
`danger-full-access`, the single-concurrency queue. That is right for work
and wasteful for conversation. With the chat tier enabled, Conveyor answers
conversation directly and keeps Codex for execution.

```text
message
  ├─ command / confirmation / stop / memo ............ as before
  ├─ deterministic or hybrid tool route .............. as before
  ├─ clear execution, or about the operator's systems  → Codex job
  └─ everything else ................................. → chat model (streamed)
                                                            ├─ answer
                                                            └─ [[ESCALATE]] → Codex
```

`/run` and `/fix` always go to Codex. `/deep` (or the "🔍 用 Codex 处理"
button) re-runs the last chat request on Codex.

## Enable

```bash
CONVEYOR_CHAT_MODE=auto
CONVEYOR_CHAT_BASE_URL=https://api.minimaxi.com/v1   # any OpenAI-compatible endpoint
CONVEYOR_CHAT_API_KEY=...
CONVEYOR_CHAT_MODEL=...
CONVEYOR_CHAT_VISION=false   # true if the model accepts images
WEB_SEARCH_BACKEND=brave     # recommended: grounds time-sensitive answers
```

Unset chat values fall back to `MINIMAX_BASE_URL` / `MINIMAX_API_KEY` /
`MINIMAX_CHAT_MODEL`. With the mode `off`, or without a key and model,
behavior is exactly the previous one. Any chat failure (network, timeout,
HTTP error, empty answer) falls back to Codex.

## Routing rules (`handlers/chat.py`)

1. Mentions of the operator's own systems (我的项目 / 这台机器 / my repo …)
   → Codex: only tools can know those facts.
2. Knowledge questions (怎么 / 如何 / 为什么 / what is / how to …) → chat,
   even when they name an action ("如何删除 git 分支").
3. Imperatives (修复 / 部署 / 跑一下 / 删除 / fix / deploy / run …) → Codex.
4. Everything else → chat; the model escalates when it needs tools.

Replies about quotes and images follow the same rules; images use the chat
tier only when `CONVEYOR_CHAT_VISION=true` (sent as `image_url` data URLs).

## Hallucination guards

The chat model cannot check anything, so the guards do not rely on it
behaving:

| Guard | Mechanism |
| --- | --- |
| No answers about the operator's systems | rule 1 routes them to Codex; the system prompt requires `[[ESCALATE]]` for anything needing tools |
| Grounding | fact-checks and time-sensitive questions (最新 / 今天 / 价格 / news …) get a web evidence pack first when search is configured |
| Model-requested search | when a search backend is configured and no evidence was pre-fetched, the model may answer `[[SEARCH: <query>]]` instead of guessing; Conveyor shows "🔎 搜索：…", runs one search and asks again with the results (one round max; the query is capped at 200 chars and only goes to the operator-configured search backend). A failed search marks the answer as unverified |
| Unverified freshness | time-sensitive answers without evidence get "ℹ️ 未联网核实，信息可能过时" |
| No invented links | every URL in an answer must appear in the evidence, the question or the quote — others are replaced with "(链接已移除)" and counted (code check) |
| Self-graded support | the model ends with `[[CONFIDENCE: high/medium/low]]`; low adds "⚠️ 把握不大" and a /deep button |
| No fake actions | the system prompt forbids claiming to have run / checked / changed anything |
| Low temperature | 0.2 |

Each answer logs `chat tier answered … confidence=… removed_links=…`, so
the rate of low-confidence answers and removed links can be watched. If it
stays high, switch the mode back to `off` or use a stronger model.

## Escalation safety

The Codex task is always built from the operator's own words (plus the
quote / image context as untrusted data), never from model output. When the
request carried untrusted content (a quote or an image), an escalation does
not start a job by itself: the bot asks, and the operator confirms with
`/deep`. Injected text in a quoted message therefore cannot make the bot
run an agent job.

## Memory & Persistence

Each chat retains the last `CONVEYOR_CHAT_HISTORY_TURNS` exchanges in an active
conversation window. Turns, session state, and `/deep` escalation requests are
persisted to SQLite (`chat_memory.db` in `settings.codex_memory_root`), so
conversations survive service restarts and deployments.

* `/chat_clear` (or `/forget`): clears the active conversation history and session state.
* Follow-ups within the active session window carry the recent turns.
* Stale turns beyond session TTL are archived; `/deep` requests persist across restarts.

## Proactive Topic Watches

Operators can subscribe to topics or search queries for proactive push notifications:

* `/watch <topic> [hours]` — subscribe to automatic web search monitoring (default: every 6h).
* `/watches` — view all active topic subscriptions.
* `/unwatch <id>` — cancel a subscription.

`conveyor-scheduler.timer` checks due watches on each tick
(`personal_tools/topic_watch.py`):

* **What counts as new**: each result is identified by its normalized URL (scheme,
  `www.`, fragment, trailing slash and tracking parameters such as `utm_*` ignored).
  The watch remembers the last 300 URLs it delivered (`topic_watches.seen_urls`), so
  re-ranked or re-titled results never trigger a push; only unseen URLs do.
* **Brief**: with the chat tier configured, the chat model writes a 2-3 sentence brief
  of what the new results say, from their titles and snippets only (no added facts,
  links stripped, results treated as untrusted). It can answer `[[SKIP]]` when the new
  results are off-topic or not substantive — then nothing is pushed and those URLs are
  remembered. Without a chat model, or if the call fails, the new results are listed.
* **Message**: the first check sends "🔔 开始关注" with the current sources; later
  pushes list only the new sources (up to 5).
* Watches created before this change take their next check as a silent baseline, so
  the upgrade does not re-push old news.
* Delivery is Telegram-only for now; Feishu watches are rejected at creation.

Smoke suites: `scripts/chat_tier_smoke.py`, `scripts/chat_memory_smoke.py`, `scripts/topic_watch_smoke.py`.
