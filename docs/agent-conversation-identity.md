# Agent, conversation, and channel identity

The operator asked how to distinguish different Agents and the conversations
held in different places. The product model is:

- Agent: assistant identity, instructions, tool permissions and scoped memory.
- Conversation: the history/context selected for a particular topic.
- Channel: where a message entered, such as Telegram, Feishu or Web.

Current storage has one durable Web conversation per named Agent. An authorized
private Telegram/Feishu binding joins that existing conversation; the default
assistant's unbound channel chat remains independent. This update describes
that behavior explicitly; it does not silently split or merge existing history.
Multiple topic conversations per Agent require a separate session-selection
extension. The operator has been asked whether channel entries should default
to independent conversations with explicit continuation or automatic sharing.

## Implemented first slice

- `/agent` without arguments, “当前助手”, or “我在和谁聊天” shows the current
  assistant, entrance and real history-sharing boundary without changing it.
- `/agents`, “切换助手”, or “有哪些助手” shows available assistants, marks the
  selected one and provides exact switch commands. Switching reports its new
  context boundary and does not move previous messages.
- Existing owner-only private-chat authorization and feature flags remain.
  Quoted text and attachments never become natural-language selection controls.
- New transcript messages record an allowlisted origin channel in existing
  metadata. Mirrored channel turns preserve Telegram/Feishu provenance in the
  Agent Web conversation instead of being presented as Web-origin messages.
- The Web chat shows current assistant identity and source labels for user and
  assistant turns. Historical messages without provenance say “来源未记录”.
  No old origin is inferred and no historical content is rewritten.

## Next interaction model

Prefer an Agent picker followed by that Agent's conversation picker. A selected
conversation header should show its title and continuation/sharing scope, with
an explicit “接着已有会话聊” action for cross-channel continuation. Switching
Agent should restore that Agent's selected conversation, not copy the previous
Agent's context. External application conversations require explicit selection
or import; merely connecting a channel must not ingest unrelated histories.

Native Telegram/Feishu picker buttons and multiple topic conversations are not
implemented by this first slice. The existing text switch commands remain the
actual selection mechanism. No channel messages or production changes were
made during this implementation.

## Verification

77 backend regressions passed, including Agent authorization, scoped history,
mirrored source metadata, transcript storage, chat history and delegation.
The Agent browser suite passed five cases including a mixed-origin conversation
and a historical message whose origin is unknown. Typecheck/build passed.
