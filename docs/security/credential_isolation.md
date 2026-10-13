# Credential isolation for child processes

Status: **opt-in**, default off. Set CONVEYOR_ENV_ISOLATION=1 for an
installation only after testing the active Codex/Claude provider locally.
Do not enable by default in this PR.

## Threat model and design

A coding agent, its tools, or hostile repository content may inspect child
process environment variables. The parent holds Web Console, Telegram, Feishu,
GitHub, cloud and provider secrets; these should not be inherited by Codex,
Claude Code, desktop planners, job Git helpers, or GUI browser helpers.

When enabled, subprocess_env.py builds a **fresh dict from a named allowlist**.
Basic OS variables are PATH, HOME, LANG, LC_*, TERM, TMPDIR, USER, LOGNAME,
SHELL, selected TLS certificate paths and XDG paths. Recognized non-secret
Codex and model endpoint configuration names also pass. There are no wildcard
provider/child prefixes. A safety-net deny regex blocks unexpected TOKEN,
SECRET, PASSWORD, API_KEY, SESSION, CREDENTIAL etc. variables and all
TELEGRAM_*, FEISHU_*, LARK_*, GITHUB_*, GH_TOKEN, AWS_*, GOOGLE_*,
GMAIL_* and CONVEYOR_* credentials, even if a future caller requests them
as an extra.

Only the selected provider's explicitly named API credential may pass:
Codex derives its env_key from the current provider configuration, and Claude
Code may receive ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN. Other provider API
keys, including DEEPSEEK_API_KEY for an OpenAI job, are removed. A model
credential cannot be used to grant control-plane tokens. No arbitrary
extra prefixes are accepted while isolation is enabled.

**Compatibility:** if the flag is off, the established
security.secrets.child_env_from logic and provider-key scoping flag retain their
existing behavior. Non-agent subprocesses with no explicit env keep the
inherited environment. This change is *not* intended to alter parent-process
environment, existing human approvals or network egress.

Current integration points:
- runner/operators/run.py: direct Codex job and provider-aware worktree;
- runner/claude_code.py: Claude Code job;
- desktop_computer_planner.py: standalone desktop Codex planner;
- runner/worktree.py and runner/operators/jobs.py: job's git worktree and apply;
- desktop_linux_browser.py: browser/xdotool children (GUI variables explicitly
  granted). agent_desktops.py already constructs its own limited GUI env;
- MCP stdio children already have strict, independent allowlist and per-server
  MCP_ allowlist; existing MCP filtering is preserved rather than weakened.

A startup self-check logs only the **names** of sensitive variables actually
planned for the selected model child, with the value always represented as
REDACTED. No secret value should ever be logged.

## Private Web Console Secrets

The Bearer-authenticated Secrets tab and endpoints allow setting credentials
without placing their **values** in the UI source or console API output:

- POST /api/secrets with JSON {"name":"OPENAI_API_KEY","value":"..."}
  -> 201 with name, set, updated_at (never returns value).
- GET /api/secrets -> {"items":[{"name":"...","set":true,"updated_at":"..."}]};
  values are never returned.
- DELETE /api/secrets/OPENAI_API_KEY -> 200; removes the stored value,
  retaining an unset metadata tombstone. Repeated deletion -> 404.

Names must match ^[A-Z][A-Z0-9_]{1,63}$. Values are nonempty UTF-8,
maximum 8192 bytes and cannot contain NUL. Malformed names and payloads are
rejected with 400. All three endpoints require the same Web Console Bearer
authorization as other /api/ endpoints (401 on failure). API JSON responses
use Cache-Control: no-store.

CONVEYOR_SECRETS_FILE chooses the JSON store. Default:
CODEX_MEMORY_ROOT/secrets/console_secrets.json. On each write, the store
creates a private temporary file in the same directory, fchmod(0600),
fsyncs, then os.replace (atomic on the same filesystem); temp files are
removed on error. Existing group/world-readable files and symlink file paths
are rejected. The directory should be protected (0700). Keep file and
backups out of git and outside any agent worktree.

The store is not an encryption-at-rest vault: filesystem permissions, backup
policy and physical host trust still matter. When the chosen provider key is
not in the parent environment, the isolated runner may read that **single**
explicitly granted key from the configured secrets file. Other stored secrets
do not enter children. Existing processes do not retroactively inherit
updated values; the next job uses current values.

## Example

Set these in the host's private configuration, never in repository files:

    CONVEYOR_ENV_ISOLATION=1
    CONVEYOR_SECRETS_FILE=/opt/conveyor-private/secrets.json

Open the Web Console > Secrets, create OPENAI_API_KEY using the password input,
and confirm GET /api/secrets returns only metadata. Select the corresponding
provider configuration and run a trivial Codex job. Inspect the redacted
startup diagnostic and verify jobs, approvals, and git apply still function.

## Rollout and later default-on plan

1. Keep this PR off by default; run the new unit/HTTP tests, baseline CI and
   local/VPS smoke. Verify no bot/GitHub/cloud secret inherited by any agent
   process, including a mocked child that prints its environment.
2. Pilot on a single operator with its actual provider, custom cert chain,
   proxy, shell, Linux browser and git worktree workflow; add **exact names**
   to the allowlist when necessary (never blanket prefixes).
3. Audit process creation and incident/debug logs for secrets; verify existing
   MCP strict-env contract and every platform's smoke tests.
4. Only in a separate reviewed PR switch the default to on, with documented
   rollback by explicitly setting CONVEYOR_ENV_ISOLATION=0.

Environment isolation is one layer. Host credentials on accessible filesystem
paths, inherited open file descriptors, network access and user-approved
secrets access may still expose data. It does not replace sandbox isolation
or the per-tool approval controls.
