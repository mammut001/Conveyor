# Parallel Read-Only Subagents (`agents.parallel`)

The chat tier can fan out complex, multi-faceted research questions into **parallel, read-only subagents**, each running an independent conversation with a restricted tool set to gather facts concurrently before the parent model synthesizes a final answer. Typical use cases include comparing options ("compare lib A, B and C"), status checks across repositories ("check CI, open PRs, and today's calendar"), and multi-source research.

Subagents are strictly read-only and can never perform actions or trigger side effects.

## Enable & Configuration

Subagents are disabled by default. Enabling requires `CONVEYOR_CHAT_TOOLS=true`.

```bash
# Enable parallel subagents (default: false)
CONVEYOR_SUBAGENTS_ENABLED=true

# Maximum tasks per agents.parallel call (default: 4, clamp: 1-6)
CONVEYOR_SUBAGENTS_MAX_TASKS=4

# Process-wide concurrent subagents limit (default: 3, clamp: 1-6)
CONVEYOR_SUBAGENTS_MAX_PARALLEL=3

# Per-subagent timeout in seconds (default: 90, clamp: 10-300)
CONVEYOR_SUBAGENTS_TIMEOUT_SECONDS=90

# Maximum tool rounds per subagent (default: 3, clamp: 0-6)
CONVEYOR_SUBAGENTS_MAX_STEPS=3

# Output char limit per subagent response (default: 2500, clamp: 500-8000)
CONVEYOR_SUBAGENTS_MAX_OUTPUT_CHARS=2500
```

System status (`GET /api/system/status`) reports `"subagents": true|false` under `features`.

## Tool Specification: `agents.parallel`

When `CONVEYOR_SUBAGENTS_ENABLED=true`, the `agents.parallel` tool is exposed to the top-level chat agent.

- **Danger Level**: `READ` (auto-runs without confirmation because subagents are restricted exclusively to `READ` tools).
- **Arguments**: A JSON object matching the following schema:

```json
{
  "tasks": [
    {
      "title": "short label (<=60 chars)",
      "prompt": "self-contained instruction (<=2000 chars)"
    }
  ],
  "context": "optional shared background (<=2000 chars)"
}
```

- **Validation**:
  - `tasks` must contain between 1 and `CONVEYOR_SUBAGENTS_MAX_TASKS` items.
  - `title` and `prompt` must be non-empty strings without control characters.
  - Exceeding task or length limits returns an immediate error message to the model without executing.

### When the Model Uses It
- Multiple independent research or lookup tasks that benefit from concurrent execution.
- NOT for single simple lookups or sequential workflows where task B depends on task A's output.
- Because subagents have no access to the parent conversation history, `prompt` and `context` must be completely self-contained.

## Safety Model & Constraints

| Dimension | Policy & Implementation |
| --- | --- |
| **Tool Restrictions** | Subagents inherit the parent's exposed tools (including network allowlists and MCP read tools), **strictly filtered to `DangerLevel.READ`** and excluding `agents.*`. |
| **No Side Effects** | If a subagent attempts to call a `WRITE`, `DESTRUCTIVE`, or unknown tool, the request is immediately rejected with `"not allowed for subagents"`. It never prompts for confirmation and never executes. |
| **No Recursion** | A context variable depth guard (`_SUBAGENT_DEPTH`) enforces that `agents.parallel` is only exposed to and callable by depth 0 (the top-level agent). Subagents cannot spawn child subagents. |
| **Clean Context** | Each subagent starts with a fresh system prompt instructing it to be concise, cite tools used, and refuse actions. Subagents do not receive conversation history, long-term memory, or preloaded skills. They inherit the parent's `ChatConfig` (provider endpoint, model, temperature). |
| **Untrusted Output** | Subagent outputs are treated as untrusted data. Tool results returned to the parent are sanitized, redacted via `redact_text`, and wrapped in `<tool-result untrusted="true">`. |
| **Fault Isolation** | Each task runs in an isolated `asyncio.wait_for` wrapper. A timeout or failure in one subagent does not fail peer subagents. The timeout starts when the subagent obtains a `MAX_PARALLEL` slot (`queued` → `running`), so waiting for a slot never counts against it. If the parent request is cancelled, all child tasks are cancelled. |
| **Concurrency Control** | Process-wide concurrency is bounded by a lazy `asyncio.Semaphore` keyed to the active event loop, preventing resource exhaustion across concurrent requests. |
| **Result Caps** | Individual subagent answers are capped to `CONVEYOR_SUBAGENTS_MAX_OUTPUT_CHARS` with `... [truncated]`. The combined result returned to the parent is bounded up to 16,000 characters. |
| **Audit Trail** | In addition to the top-level tool event, each completed subagent emits an audit log event (`audit_tool_event`, `action="subagent"`, tool `agents.parallel`, preview of status, elapsed time, and tools used). Full prompts and raw outputs are not logged. |

## Progress & Web Console UI

- **Bot Channels (Telegram / Feishu)**: Live progress updates are sent via `port.edit_progress` (e.g., `"🧩 子任务 2/3 完成 …"`).
- **Web Console**: An SSE event `event: subagent` is emitted with:
  ```json
  {
    "call_id": "call_xyz",
    "index": 0,
    "title": "Query CI status",
    "status": "queued|running|ok|timeout|error",
    "elapsed": 1.4,
    "tools": ["web_fetch"]
  }
  ```
  - `subagent` SSE events are transient and **never persisted** into transcript memory.
  - In the Web Chat interface, active subagents appear as a compact progress card group beneath the assistant message turn.
  - Upon completion, the card group collapses into an unobtrusive summary badge (e.g., `3 个子任务 · 3 ✓`), expandable on click.
  - Fully responsive on mobile layouts (360–430px screens) without horizontal overflow.

## Notes

- Subagent progress cards are live-only: they are not stored in the transcript, so a reloaded session shows the final answer without the card group.
- Non-Web channels get a `🧩 子任务 n/N 完成 …` placeholder edit; the Web port receives structured `subagent` SSE events instead (a plain edit there would be taken as the final answer).
- Web tools (`web.search`, `web.fetch`, …) are only available to subagents if they are already allowlisted for the parent via `CONVEYOR_CHAT_TOOLS_NETWORK_ALLOW`.
