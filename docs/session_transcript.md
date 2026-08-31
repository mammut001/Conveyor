# Structured session transcript

Conveyor keeps two intentionally separate session representations:

1. `handlers/session.py` JSONL remains the small bounded prompt-context cache used to make follow-up requests such as “continue” useful.
2. `transcript_store.py` stores durable, redacted user/assistant messages in SQLite for the Web Console and future clients.

The transcript is not hidden reasoning. It stores only user-visible conversation text and optional display metadata.

Session IDs are namespaced by channel, operator and source chat ID, so identical
Telegram and Feishu chat identifiers cannot collide. `/forget` retains its
historical meaning: it clears only the bounded JSONL prompt-context cache, not
the durable UI transcript.

This separation prevents UI/history requirements from silently changing what is injected back into model prompts.
