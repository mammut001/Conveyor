# Repository layout

Application implementation lives under `conveyor/`. The root keeps standard project metadata and dependency configuration. Python service entrypoints live under `conveyor/entrypoints/`.

| Directory | Responsibility |
|---|---|
| `conveyor/agents/` | Agent identity, memory, Supervisor, goals, routing and worker pool |
| `conveyor/automation/` | Routines, event triggers, durable workflows and overnight runs |
| `conveyor/browser/` | Browser workflows, authentication and network policy |
| `conveyor/channels/` | Telegram helpers, voice and approvals |
| `conveyor/desktop/` | Computer actions, observations, live screen and takeover |
| `conveyor/web/` | Web API helpers, chat and control services |
| `conveyor/core/` | Providers, process environment, transcripts and runtime state |
| `agent_coordination/` | Delegation queues, channel routing and result delivery |
| `channel/`, `handlers/`, `runner/`, `personal_tools/` | Existing channel adapters, request handlers, execution backends and tools |
| `web/`, `menubar-agent/` | Browser and macOS client applications |
| `scripts/`, `systemd/`, `deploy/` | Development commands and deployment configuration |
| `tests/`, `docs/` | Regression coverage and documentation |

## Python imports

Use the full package path for implementation modules:

```python
from conveyor.agents import agent_supervisor
from conveyor.browser import egress_policy
from conveyor.desktop import live_screen
```

Invoke services with `python -m conveyor.entrypoints.<name>` from the repository root. The old `runner.py` shim is replaced by the existing `python -m runner` command. See [entrypoint migration](entrypoint-migration.md) before updating an existing installation. Overnight services use `python -m conveyor.automation.overnight_cli` and `python -m conveyor.automation.overnight_control`.

Python implementation modules are imported through their package paths, including `conveyor.core.config`, `conveyor.core.redaction`, and `conveyor.core.logging_setup`. Source-file locations do not change configured storage directories or database schemas. Deploy the complete revision together; the rsync deployment list includes `conveyor/` as a whole.

The MCP example is in `deploy/examples/mcp_servers.toml`; historical project notes are in `docs/project-history.md`. Standard `requirements.txt`, README files, contribution policies and the Makefile remain at the root for tooling and repository navigation.
