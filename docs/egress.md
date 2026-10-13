# P3-4: egress proxy and tainted outbound gate

## Activation (default OFF)

Set:

```env
CONVEYOR_EGRESS_PROXY=1
CONVEYOR_EGRESS_ALLOWLIST=api.openai.com,*.github.com,localhost
# Optional loopback listener port (0 = OS-chosen ephemeral port)
CONVEYOR_EGRESS_PROXY_PORT=0
# Required for tainted outbound approval inbox:
CONVEYOR_APPROVAL_INBOX_ENABLED=1
# Optional editable email/GitHub/MCP draft cards:
CONVEYOR_DRAFT_CARDS=1
```

Unless `CONVEYOR_EGRESS_PROXY=1`, the proxy is **not started**, no proxy environment
is injected, and no new taint state is recorded or consulted. Empty egress
allowlist means **deny all**, not allow all. Comma-separated domains are
case-insensitive: exact `example.com`, and wildcard `*.example.com`
matches subdomains but **not** `example.com` itself. This allowlist controls
network destinations; it is different from `CONVEYOR_OUTBOUND_ALLOWLIST`
which limits *recipients* of editable draft cards.

The proxy binds **only to 127.0.0.1** and uses the Python standard library.
It accepts absolute-form HTTP requests and HTTPS `CONNECT host:port`, checks
the domain before opening a connection, resolves all DNS answers, and pins the
connection to the validated numeric address. Non-allowlisted destinations are
rejected before DNS. Private, loopback, link-local, unspecified, reserved, and
other non-global DNS results are rejected for wildcard entries. **Only an
explicit exact allowlist entry** (including an exact IP literal or hostname)
may connect to a private address, useful for test fixtures; do not add a local
address in production.

Each policy decision is written as JSONL to
`<codex_memory_root>/egress_decisions.jsonl` (mode 0600):
`time`, `host`, `port`, `decision`, `reason`. There are **no request
bodies, response bodies, URLs paths, query strings, or TLS payloads** in this
log. Protect and rotate this file under the deployment's normal log policy.

When enabled, `subprocess_env.utility_env` and `agent_env` inject
`HTTP_PROXY`, `HTTPS_PROXY` and lowercase variants, removing any inherited
`NO_PROXY` and `ALL_PROXY`. The Codex/Claude job child environment also
goes through this helper, irrespective of whether
`CONVEYOR_ENV_ISOLATION=1`. The feature does not change credential isolation
or the child command line. Proxy startup is lazy on the first child env
request.

**Security boundary:** This is a *cooperative* forward proxy. Programs that
ignore proxy environment variables, open raw sockets, or unset these variables
are **not** confined by it. It is not a system-wide firewall or a substitute
for network namespace/firewall egress restrictions. It does not see TLS
plaintext and cannot inspect sensitive bytes in opaque network traffic,
HTTP headers, or arbitrary tool implementations. In particular, the taint
gate described below applies to registered outbound tool actions, **not**
arbitrary network commands run by Codex. Do not claim OS-level exfiltration
prevention. For strict enforcement, deploy with a process/container network
policy blocking direct egress and permitting only the proxy.

## Private-read taint (same flag)

Session taint markers are stored, with no private content, in
`<codex_memory_root>/egress_taint.db` (mode 0600), keyed by operator,
channel, and chat ID. Attempts to read `memory.*`, `gmail.*`, inbox
or secret tools mark the session (including failed reads, conservatively).
Taint survives server restarts and remains until an explicit session reset.
New sessions and different operators are not automatically tainted.

Registered outbound sends are classified with the existing
`outbound_draft_cards.is_outbound` helper: `email.send`,
`github.comment`, `github.create_issue`, and explicitly declared
outbound MCP WRITE/DESTRUCTIVE tools. After a private read, a send must go
through the existing confirmation/Approval Inbox or draft-card path; without
either approval surface it is refused. The low-level `run_tool` API rejects
such sends when a context is present and no verified approval is supplied.
Human-confirmed execution uses the established approval token flow and is
allowed to proceed exactly once. The default-off switch skips this check
entirely.

This intentionally simplified taint check does **not** follow tainted bytes
through arbitrary functions, Codex shell commands, direct SMTP transport
invocations, or third-party tools misclassified as READ. Keep outbound-tool
classifications accurate and use OS network isolation for a stronger policy.

## API

Authenticated `GET /api/egress` returns:

```json
{"enabled": true, "allowlist": ["api.openai.com"], "recent_denials": []}
```

The endpoint is read-only and uses the Web Console's existing Bearer token
authorization (401 without it), with no ability to modify the policy.

## Verification

```sh
python scripts/egress_smoke.py
python -m unittest discover -s tests -v
npm --prefix web run build
```

Smoke runs against local echo servers; it makes no external network requests.
Test both default-off operation and the flag-on isolated workflow before
enabling in production. Do not expose the loopback proxy over a public
forwarder.
