# Independent entries and explicit conversation continuation

The operator chose: each entry keeps independent history by default; explicitly
select an existing conversation to continue it. Agent identity, conversation
history, and message origin are separate concepts.

## Behavior

- Telegram, Feishu and Web have independent initial conversations for each
  Agent. Selecting an Agent restores that entry's last selected conversation
  for that Agent. Switching Agent does not copy any previous Agent's history.
- In a private phone chat, “接着已有会话聊” or `/conversations` lists only the
  current Agent's conversations. `/conversation <reference>` explicitly joins
  one. `/conversation local` returns to this entry's independent root;
  `/conversation new <title>` starts a new topic.
- The Web Agent workspace has a conversation picker and an “另开话题” action.
  Selection and drafts persist separately per Agent/conversation in the browser.
  A pending reply disables the picker; an unreadable list offers retry or an
  explicit fallback to the original Web conversation.
- Current Agent cards state the selected conversation. New transcript messages
  retain Telegram/Feishu/Web provenance. Legacy records without provenance are
  not relabeled with an inferred origin.

## Storage and compatibility

The existing control-plane database gains two additive tables:
`agent_conversations` and `agent_conversation_selections`. Selection is keyed
by physical channel, chat, operator and Agent. New conversation IDs are opaque;
short list references are selectors rather than credentials.

Existing Agent Web conversations retain their session IDs, history keys, and
context files. They appear as “Web 会话（原有记录）” and can be explicitly
continued. Prior shared records remain there. Existing raw channel transcripts
and legacy context files are not deleted or rewritten; mixed legacy channel
logs are not automatically imported into a new Agent conversation. The default
assistant's own legacy entry-specific chat cache and JSONL context keep their
existing keys/paths. They remain usable in that entry and via explicit
continuation, without importing the mixed physical channel transcript.

New phone conversations have a Web-safe transport alias for viewing/continuing
from the authenticated console. Server-side alias lookup derives the Agent and
capabilities. A Web token cannot adopt a phone operator or transport identity.

History can be rebuilt from the selected durable transcript after short-term
cache expiry. Rebuild is bounded to the configured turn count (maximum 20 turns,
4,000 characters per message). Clearing context writes a reset boundary and
clears that conversation's cache/context file; older transcript records remain
visible but are not silently reintroduced into model context.

## Authorization and in-flight behavior

Private channel selection still requires the configured owner, a live Agent,
and the existing Agent routing/context flags. Group/non-owner controls expose
no conversation list. References for a different Agent, an unknown/archived
Agent, or a previous channel owner are rejected. A selection whose source owner
is no longer valid falls back to the current entry's independent root.

Conversation selection is pinned for an inbound request across awaits. A reply
finishing after a selection change is saved to its originating conversation.
Queued tasks persist the originating conversation in existing job metadata;
recovery pins it again and revalidates Agent/owner authority before execution.
Changing Agent cannot cause queued work to execute with the new Agent's rights.

Conversation continuation shares chat context only. Task refinement/worktree
identity also includes the physical channel/operator scope, so joining a phone
conversation from Web does not transfer that entry's task-worktree authority.
Agent instructions, tool grants and explicit long-term memory retain their
Agent scope; independent conversation history is distinct from Agent memory.

No production migration, restart or outgoing Telegram/Feishu message has been
performed. The routing feature remains behind its existing enablement flags.

## Direct channel selection

With named-Agent routing enabled, `/agent` (or “当前助手”) opens a current-context
card. `/agents` (“切换助手”) offers Agent buttons, and `/conversations`
(“接着已有会话聊”、“切换会话”或“会话列表”) offers this Agent's conversation buttons. Each choice shows
its origin entry and the selected item is marked. Both menus support eight
choices per page. “另开新话题” starts immediately, while “回到本入口” restores the
independent local root. Custom titles remain available through
`/conversation new <标题>`.

Telegram uses inline buttons; Feishu uses its existing `cardAction` event
subscription. Existing legacy Telegram “我的 Workers” and “切换会话” reply-keyboard
labels open these selectors when owner-private routing is enabled. The explicit
`/workers` and `/sessions` read-only task views remain available.

Button payloads contain only opaque server-issued references (under Telegram's
64-byte limit). References expire after 15 minutes, are one-use, and are bound
to channel, operator and physical private chat. Topic actions also check the
Agent that issued the card. Forwarded Feishu cards cannot acquire private-chat
authority; duplicate deliveries cannot create duplicate topics. Live Agent and
conversation ownership checks still apply when a button is pressed. Feature-off
and noninteractive transports retain the original text-command path. Failed
interactive delivery provides executable text choices as a fallback.
