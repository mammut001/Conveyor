# Experimental Claude Code backend

Conveyor can construct an opt-in `ClaudeCodeBackend` that reuses the existing worktree, job metadata, cancellation and explicit Apply/Discard lifecycle while replacing the provider attempt with the locally authenticated Claude Code CLI.

It is **not selected by existing entrypoints by default**. `CodexRunner` remains the production default. Callers that intentionally adopt `create_agent_backend(...)` can select `CONVEYOR_AGENT_BACKEND=claude-code`.

Environment knobs:

- `CONVEYOR_CLAUDE_BIN` — defaults to `claude`
- `CONVEYOR_CLAUDE_MODEL` — optional Claude Code model/alias
- `CONVEYOR_CLAUDE_PERMISSION_MODE` — `acceptEdits` (default) or the more restrictive `plan`; other values fail validation

The backend uses Claude Code print mode with stream JSON and partial messages. It relies on the operator's existing local Claude Code authentication; Conveyor does not accept or persist an Anthropic API key for this backend.

Security and compatibility:

- Ask/Run jobs use `plan` with read-only file tools; Fix jobs use only read/search/edit/write file tools in the detached worktree;
- shell, Web, MCP, plugins, hooks, session persistence and permission bypass are unavailable to the child process;
- hidden thinking deltas and raw Claude envelopes are never persisted;
- logs/events contain only bounded, redacted text and compact tool state; tool inputs and tool-result content are discarded;
- prompts are sent over stdin rather than exposed in the process argument list;
- the child receives Conveyor's filtered execution environment instead of the service's complete environment;
- execution still occurs inside Conveyor's detached worktree and changes still require the existing Apply policy;
- the backend is experimental until it receives the same VPS/live validation coverage as Codex.
