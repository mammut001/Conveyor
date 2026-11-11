# Service entrypoint migration

The root Python launchers now live in `conveyor/entrypoints/`. Run services as modules from the repository root:

| Previous launcher | New command after the Python interpreter |
|---|---|
| `bot.py` | `-m conveyor.entrypoints.bot` |
| `feishu_bot.py` | `-m conveyor.entrypoints.feishu_bot` |
| `web_console_takeover.py` | `-m conveyor.entrypoints.web_console_takeover` |
| `web_console.py` | `-m conveyor.entrypoints.web_console` |
| `desktop_agent.py` | `-m conveyor.entrypoints.desktop_agent` |
| `desktop_agent_server.py` | `-m conveyor.entrypoints.desktop_agent_server` |
| `desktop_chat_window.py` | `-m conveyor.entrypoints.desktop_chat_window` |
| `agent_desktops.py` | `-m conveyor.entrypoints.agent_desktops` |
| `handoff_sidecar.py` | `-m conveyor.entrypoints.handoff_sidecar` |
| `runner.py` | `-m runner` |

Existing flags and environment settings remain in use. The versioned unit templates and desktop launchers use the module commands. Run `scripts/run-desktop-agent.sh` for the configured local desktop agent.

## Existing installations

This release requires a maintenance cutover of source and startup configuration. The candidate smoke suite reads effective systemd commands and rejects a deployment that still points at removed root launchers. It does not edit installed units or restart services.

Before updating a live installation:

1. Inspect active services, pending jobs and effective unit commands, including drop-ins. Back up the current revision, database, installed unit files and overrides.
2. Drain work and stop the affected services and producers during the maintenance window. Keep their previous active/inactive states so unrelated services are not enabled accidentally.
3. Update source and the relevant installed startup commands together. Preserve local unit customizations, arguments, environment files, users and working directories. Use the table above; Python module commands require the repository root as their working directory.
4. Reload systemd, run smoke and configuration checks, and restore only the previously active services. Check their status, actual listening ports, authenticated API responses and task state before releasing the maintenance fence.
5. If cutover fails, restore both the previous source revision and startup configuration, reload systemd, and verify the restored services. A source-only rollback is insufficient for this entrypoint migration.

Do not update the unit commands while the old source is still serving requests: an automatic restart could try to import modules that have not yet been installed. Future deployments can use the regular workflow after this one-time migration.

## macOS desktop agent

Rebuild/reinstall the menu bar app from `menubar-agent/` when updating this checkout; the new supervisor starts the desktop agent by module name and detects the new source path. If a desktop-agent LaunchAgent is installed, regenerate it from the updated template so it points to `scripts/run-desktop-agent.sh`. The environment file remains `.desktop-agent.env` at the repository root. Existing user-created launch commands must adopt the module invocation as well.
