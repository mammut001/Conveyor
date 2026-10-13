# MCP client: TOML allowlists and approval levels

Conveyor already contains an MCP client, a chat tool bridge, a unified approval inbox, and authenticated connector status endpoints. This change extends those components; it does not start a second MCP subsystem.

## Enable and configure

MCP remains **off by default**. Nothing is started or exposed unless `CONVEYOR_MCP_ENABLED=1`. Configure `CONVEYOR_MCP_CONFIG=/absolute/path/mcp_servers.toml`; otherwise the existing settings loader prefers `<codex_memory_root>/mcp_servers.toml` when present, and preserves legacy `mcp_servers.json` installations.

```toml
[servers.notes]
transport = "stdio"
command = "/usr/bin/python3"
args = ["/opt/mcp/notes_server.py"]
allow_tools = ["search", "append", "delete"]
timeout_seconds = 15
max_output_chars = 12000

[servers.notes.tool_levels]
search = "READ"
append = "WRITE"
delete = "DESTRUCTIVE"

[servers.notes.env]
NOTES_DIR = "/srv/notes"

[servers.notes.env_from]
NOTES_API_TOKEN = "MCP_NOTES_API_TOKEN"

[servers.remote]
transport = "streamable_http"
url = "https://example.com/mcp"
allow_tools = ["lookup"]

[servers.remote.tool_levels]
lookup = "READ"

[servers.remote.headers_from]
Authorization = "MCP_REMOTE_AUTHORIZATION"
```

The optional `env_allowlist = ["MCP_FOO"]` list passes explicitly named variables into a stdio child under the same names. For renamed variables, use `[servers.<name>.env_from]` as shown. The environment of a **TOML-configured** child consists **only** of literal `env` entries and explicitly declared `MCP_*` variables; it does not inherit the parent's `PATH`, `HOME`, `LANG`, bot tokens, or other secrets. The working directory is a dedicated permission-restricted directory when no `cwd` is configured. The executable path must be absolute. This is environment isolation, not an OS sandbox; trusted servers still have the Conveyor user's filesystem/network permissions.

Only names in `allow_tools` are exposed, and an omitted or empty list exposes **nothing**. `tool_levels` entries must refer to allowed tools. Anything allowed but not explicitly assigned has **WRITE** danger. Remote-provided `readOnlyHint` and `destructiveHint` annotations cannot override the operator's TOML decisions. Unlike TOML, legacy JSON configurations retain their existing `read_only_tools` and `trust_read_only_hint` interpretation for backward compatibility.

## Runtime behavior

The client performs JSON-RPC 2.0 `initialize`, `notifications/initialized`, `tools/list` and `tools/call` using stdio newline-delimited JSON or HTTP JSON/SSE. There are no automatic retries of tool calls. Stdio servers run in short-lived sessions, each in its own process group, terminated and reaped on exit or timeout; discovery failures are retried only after the existing backoff interval (60 seconds) on a subsequent request. Both connection/init and call operations have bounded timeouts. Tool content is redacted and truncated, never exceeding 64 KiB in UTF-8, and chat-loop outputs are wrapped in `<tool-result untrusted="true">…</tool-result>`; page instructions or injected tool-result terminators cannot grant approval.

In model function schemas names are `mcp__<server>__<tool>`, mapped reversibly to the existing internal `mcp.<server>.<tool>` tool dispatch; original `inputSchema` object JSON schemas are forwarded. The already existing chat tool registration path is used rather than a parallel PERSONAL_TOOL_REGISTRY. READ executes without a write confirmation; WRITE and DESTRUCTIVE go through the existing approval inbox / chat confirmation. The server's allowlist and danger level are read again at execution. Do not mark a tool READ merely because its name sounds harmless: models can choose arguments, and READ tools that access network destinations may leak information.

The authenticated read-only `GET /api/mcp/servers` endpoint already reports server status, count and last error without environment values. The existing optional Web Connectors panel also supports enable/disable and refresh actions; this change does not add further controls or expose secrets.

## Verification

Run `python -m unittest tests.test_mcp tests.test_mcp_toml -v`. The new tests cover TOML syntax and policy validation, native list/call against a tiny stdio fixture, zero parent-environment inheritance, allowlist enforcement, danger-level confirmation and prompt-injection isolation, size limits, timeouts, malformed JSON, and recovery after a crashed subprocess. Existing MCP JSON and Web API regression tests must remain green.
