# MCP connectors (P2-1)

Conveyor supports Model Context Protocol (MCP) tool connectors in the chat tier (Web chat, Telegram, Feishu, routines). Operators can configure external MCP servers over **stdio** (subprocesses) or **streamable HTTP** (SSE / JSON-RPC 2.0).

MCP connectors provide external read and write actions while preserving Conveyor's core security invariants: strict environment isolation, secret redaction, output truncation, per-server allowlists, and operator confirmation for all write operations through the Unified Approval Inbox.

## Feature flags

```dotenv
# Default: false — no servers spawned, no tools exposed, API returns 409
CONVEYOR_MCP_ENABLED=false

# Optional custom path to servers configuration (default: <codex_memory_root>/mcp_servers.json)
# CONVEYOR_MCP_CONFIG=/etc/conveyor/mcp_servers.json
```

When enabled:
- Reported as `features.mcp = true` in `WebControl.system_status()`.
- The Web Console enables the **Connectors** tab next to Skills.
- Configured and allowlisted tools appear in the chat tier function schemas as `mcp.<server>.<tool>`.
- Read-only tools execute automatically within the chat tool loop.
- Modifying tools request operator confirmation and appear in the Web approval inbox.

When disabled (`CONVEYOR_MCP_ENABLED=false`):
- All `/api/mcp/*` endpoints return `409 Conflict` with `{"error": "MCP connectors are disabled (set CONVEYOR_MCP_ENABLED=true)"}`.
- No child processes are spawned and no HTTP connections are made.
- No MCP tools are exposed in chat schemas or tool dispatch.
- The Web Console hides the Connectors tab.
- Behavior is completely identical to prior versions.

## Configuration

Servers are defined in a JSON file (by default `<codex_memory_root>/mcp_servers.json`). Missing files or configuration syntax errors are logged safely without crashing Conveyor.

### Example configuration

```json
{
  "servers": {
    "notes": {
      "transport": "stdio",
      "command": "/usr/bin/python3",
      "args": ["/opt/mcp/notes_server.py"],
      "cwd": "/opt/mcp",
      "env": {
        "NOTES_DIR": "/srv/notes"
      },
      "env_from": {
        "NOTES_TOKEN": "MCP_NOTES_TOKEN"
      },
      "allow_tools": ["search", "read", "append"],
      "read_only_tools": ["search", "read"],
      "trust_read_only_hint": false,
      "timeout_seconds": 30,
      "max_output_chars": 4000,
      "enabled": true
    },
    "docs": {
      "transport": "http",
      "url": "http://127.0.0.1:9000/mcp",
      "headers": {
        "X-Tenant": "ops"
      },
      "headers_from": {
        "Authorization": "MCP_DOCS_AUTH"
      },
      "allow_tools": ["*"],
      "read_only_tools": [],
      "timeout_seconds": 20,
      "max_output_chars": 5000
    }
  }
}
```

### Schema & Validation Rules

- **Server Name**: Must match `^[a-z0-9][a-z0-9_-]{0,31}$`.
- **Max Servers**: Up to 20 servers. Servers exceeding the limit receive `status: "config_error"`.
- **Transport**: `stdio` or `http`.
- **stdio fields**:
  - `command`: Absolute filesystem path to the executable (no shell expansion).
  - `args`: List of strings (default: `[]`).
  - `cwd`: Optional directory path.
- **http fields**:
  - `url`: Valid `http://` or `https://` endpoint URL.
  - `headers`: Literal key-value dictionary (default: `{}`).
- **Secret forwarding**:
  - `env_from` (stdio) and `headers_from` (http) map child env/header names to host environment variable names.
  - **Security rule**: Host variable names **must** start with `MCP_` (e.g. `MCP_NOTES_TOKEN`). Referencing any other variable (e.g. `TELEGRAM_BOT_TOKEN`, `CONVEYOR_WEB_TOKEN`, `OPENAI_API_KEY`) is rejected as a configuration error.
- **Allowlist (`allow_tools`)**: Required list of strings. Wildcard `"*"` is allowed to expose all tools reported by the server.
- **Read-Only Tools (`read_only_tools`)**: List of strings designating tools that can auto-run without operator confirmation. Wildcard `"*"` is **not** permitted here.
- **`trust_read_only_hint`**: Boolean (default `false`). If `true`, tools reporting `annotations.readOnlyHint: true` (and not `destructiveHint: true`) are treated as read-only. Untrusted by default.
- **`timeout_seconds`**: Integer between 1 and 120 (default 30).
- **`max_output_chars`**: Integer between 200 and 20000 (default 4000).
- **`enabled`**: Boolean (default `true`).

## Security & Isolation

1. **Child process environment**:
   - stdio child processes run in isolated environments created from scratch.
   - Child processes only inherit:
     - `PATH` (from host, or `/usr/local/bin:/usr/bin:/bin`)
     - `LANG` and `LC_ALL` (if set)
     - `HOME`: Set to a private per-server directory `<codex_memory_root>/mcp_home/<name>`, created with POSIX mode `0700`.
     - Explicit literal entries defined in `env`.
     - Resolved values from `env_from` referencing host variables starting with `MCP_`.
   - Host secrets, bot tokens, and Conveyor tokens are never passed to MCP subprocesses.
2. **Output bounding & redaction**:
   - Child process stderr is bounded to a 4 KB circular buffer, redacted, and used only for diagnosing errors.
   - Stdio message lines are capped at 1 MB, and maximum session data read is capped at 4 MB.
   - All tool responses are processed through `redaction.redact_text` to strip any detected credentials or tokens before returning to the model or operator.
   - Outputs exceeding `max_output_chars` are truncated with `\n[truncated N chars]`.
3. **Network boundaries**:
   - HTTP transport uses standard Python urllib with redirects disabled (`NoRedirectHandler`) to prevent SSRF redirection.
   - HTTP responses are stream-parsed and capped at 4 MB.
4. **Execution-time policy re-validation**:
   - Even if a write tool action was requested and confirmed previously, `execute_confirmed` re-validates at execution time that:
     - `CONVEYOR_MCP_ENABLED` is true.
     - The server is still present in configuration.
     - The server is not disabled in `<codex_memory_root>/mcp_state.json`.
     - The tool remains in `allow_tools`.

## Tool Calling & Confirmation Flow

### Function Schemas

When enabled, MCP tools are converted into OpenAI-compatible tool definitions:
- **Internal name**: `mcp.<server>.<tool>`
- **API function name**: `mcp__<server>__<sanitized_tool>` (registered reversibly in `REVERSE_TOOL_MAP`).
- **Input schema**: The tool's declared JSON schema is preserved (must be an object schema ≤ 8 KB).
- **Description**: Prefixed with `[MCP <server>] ` and suffixed with `[read]` or `[write]`.

### Danger Levels

- **`DangerLevel.READ`**: Tools listed in `read_only_tools`, or tools annotated with `readOnlyHint: true` when `trust_read_only_hint: true`. Auto-runs during the chat tool loop; results are wrapped in `<tool-result name="..." untrusted="true">`.
- **`DangerLevel.WRITE`**: All other tools. Execution halts immediately and a confirmation request is created.

### Confirmation & Unified Approval Inbox

Write actions trigger confirmation buttons in chat and appear in the Web Console's **Approvals** inbox:
- Tool arguments are formatted as canonical JSON so the operator sees exact structured arguments.
- Approving executes the tool via `execute_confirmed`.
- MCP tools are not editable in the inbox (arguments are approved or rejected as-is).

## Web Console & Toggle State

The Web Console provides real-time status and control over connectors:
- **Connectors Tab**: Lists all configured servers, their transport, target (cleaned, stripping credentials and query parameters), status (`ok`, `error`, `config_error`, `disabled`), tool counts, and allowlisted tools with read/write badges.
- **Enable / Disable Toggle**: Writing to the configuration file is avoided. Instead, toggling a server updates an atomic state file `<codex_memory_root>/mcp_state.json` (POSIX mode `0600`):
  ```json
  {
    "disabled": ["notes"]
  }
  ```
  A server is effectively enabled if `enabled: true` in config AND its name is not in `disabled`.
- **Refresh**: Clicking refresh re-lists tools from the server and refreshes the memory cache (cached with a 300 s TTL).

## Endpoints

All endpoints require Bearer authentication. When `CONVEYOR_MCP_ENABLED=false`, all endpoints return `409 Conflict`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/mcp/servers` | List all servers, status, cleaned target, and tools. Leaks no secrets or environment variables. |
| `POST` | `/api/mcp/servers/<name>/refresh` | Connect, fetch tool list, and update cache. Returns 200 or 502 with server status. |
| `PUT` | `/api/mcp/servers/<name>` | Toggle server enabled state (`{"enabled": bool}`). Returns 200 with updated item. |
