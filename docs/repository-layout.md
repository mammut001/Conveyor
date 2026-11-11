# Repository layout

Application implementation lives under `conveyor/`. The root keeps project metadata, configuration, and the established service launchers.

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

Root launchers such as `bot.py`, `feishu_bot.py`, `web_console_takeover.py`, and `desktop_agent_server.py` retain their existing invocation paths. Overnight services use `python -m conveyor.automation.overnight_cli` and `python -m conveyor.automation.overnight_control` from the repository root.

The former top-level imports for moved modules are no longer supported. Scripts outside this repository that import them must adopt the package paths. Source-file locations do not change configured storage directories or database schemas. Deploy the complete revision together; the rsync deployment list includes `conveyor/` as a whole.
