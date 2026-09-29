# Installing Conveyor

Conveyor supports a one-line bootstrap on Debian/Ubuntu Linux hosts.

## Prerequisites

- a Debian/Ubuntu VPS or Linux dev box with `sudo`
- SSH access to that host
- Codex CLI installed and authenticated for the service user
- a Telegram account and bot token
- a Git repository that Conveyor may use as its workspace

## One-line install

SSH into the host, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/mammut001/Conveyor/main/scripts/bootstrap.sh | sudo bash
```

The bootstrap script only performs the network/bootstrap stage. It downloads a Conveyor source ref into a temporary directory and delegates to the repository-owned `scripts/install.sh`.

The installer then:

1. installs the required Debian/Ubuntu packages;
2. auto-detects the non-root service user when possible;
3. installs Conveyor under `/opt/conveyor` by default;
4. creates the Python virtual environment and installs requirements;
5. launches the interactive environment configurator;
6. installs parameterized systemd units;
7. installs `/usr/local/bin/conveyor`;
8. validates the configured Codex CLI;
9. runs the Conveyor smoke suite before services are restarted;
10. enables and starts the configured services.

The first-time installer requires a real interactive terminal because Telegram/API credentials are entered through `/dev/tty` rather than through the `curl` pipe.

## Install a specific release/ref

For reproducible installs, pin a tag, branch, or commit that exists in the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/mammut001/Conveyor/main/scripts/bootstrap.sh \
  | sudo CONVEYOR_VERSION=v0.3.0 bash
```

Production deployments should prefer a stable release tag instead of tracking `main` indefinitely.

## Custom install path or service user

Environment overrides may be passed to the installer process:

```bash
curl -fsSL https://raw.githubusercontent.com/mammut001/Conveyor/main/scripts/bootstrap.sh \
  | sudo CONVEYOR_DIR=/srv/conveyor CONVEYOR_USER=deploy bash
```

If `CONVEYOR_USER` is omitted, the installer prefers `SUDO_USER`, then the first normal login user, and only falls back to `root` with a warning.

## Management CLI

After installation:

```bash
conveyor status
conveyor logs
conveyor doctor
sudo conveyor restart all
sudo conveyor setup
sudo conveyor configure
sudo conveyor update
sudo conveyor uninstall
```

`conveyor update` downloads the configured `CONVEYOR_INSTALL_REF`, runs the smoke gate, and only then restarts services.

## Setup wizard (`conveyor setup`)

The installer only asks for the basics (Telegram, Codex). Everything else
is configured with the interactive wizard, on the server:

```bash
sudo conveyor setup            # dashboard: pick what to configure
sudo conveyor setup email      # one module: telegram codex chat search email feishu github web
conveyor setup --status        # what is configured (no secrets shown)
sudo conveyor setup --check    # live-test every configured integration
```

| Module | Asks for | Verified live by |
| --- | --- | --- |
| `telegram` | bot token, your user id (auto-detected when you message the bot) | Telegram `getMe` / `getUpdates` |
| `codex` | OpenAI / MiniMax key or an existing `codex login`, workspace repo, binary | git repo root check, `codex --version` |
| `chat` | chat-tier provider (DeepSeek / MiniMax / OpenAI / any OpenAI-compatible), model, key, vision | one real chat request (reports first-token latency) |
| `search` | Brave / Tavily / Serper key or SearXNG URL | one real search |
| `email` | preset (Gmail / QQ / 163 / 126 / iCloud / custom), address, app password | IMAP login + SMTP login, optional test mail to yourself |
| `feishu` | App ID / Secret, allowed open_id | tenant access token |
| `github` | fine-grained token, default repo | `/user` (+ scopes) and repo access |
| `web` | enable, port, access token (generated) | loopback binding check |

How it behaves:

- Arrow-key menus, hidden secret input (only the last 4 characters are ever
  shown), spinners during checks. Without a TTY it falls back to numbered
  prompts, so it also works over plain pipes.
- A failed check offers: re-enter, retry, save unverified, or skip the module.
- Each module shows a preview of the keys it will write (secrets masked) and
  writes only after you confirm: previous `.env` backed up (last 5 kept),
  atomic replace, mode `600`, comments and unrelated keys preserved.
- Ctrl-C / Esc exits at any time; modules already saved stay saved.
- At the end `conveyor setup` offers to restart exactly the services whose
  settings changed.

Secrets are entered only here, never in chat. In Telegram, `/setup` shows the
same module checklist with the command to run for each gap, and
`/setup_check` runs the same live tests. New operators get that checklist
right after `/onboard`.

## Security note

`curl | bash` executes network-delivered code as root. If you prefer to inspect the installer first:

```bash
curl -fsSLo /tmp/conveyor-bootstrap.sh \
  https://raw.githubusercontent.com/mammut001/Conveyor/main/scripts/bootstrap.sh
less /tmp/conveyor-bootstrap.sh
sudo bash /tmp/conveyor-bootstrap.sh
```

For production, pin a release ref and review release changes before upgrading.

## Manual/local source install

The repository-local path remains supported:

```bash
git clone https://github.com/mammut001/Conveyor.git
cd Conveyor
sudo bash scripts/install.sh
```

This uses the same real installer as the one-line bootstrap.
