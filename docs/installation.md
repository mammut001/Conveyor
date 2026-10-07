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
sudo conveyor configure
sudo conveyor update
sudo conveyor uninstall
```

`conveyor update` downloads the configured `CONVEYOR_INSTALL_REF`, runs the smoke gate, and only then restarts services.

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


### Deploy a committed local revision over SSH

For an operator-managed VPS, a Git bundle can deploy a local commit without
publishing a branch. First commit the changes and create a bundle containing
`HEAD`. Transfer it to the VPS and invoke the **candidate revision's**
`scripts/deploy_vps.sh` with `CONVEYOR_DEPLOY_BUNDLE` set to the bundle's
absolute path, `GITHUB_SHA` set to its exact 40-character commit ID, and
`CONVEYOR_DEPLOY_PATH` set to the existing installation.

The bundle must verify, match the requested commit, and extend the currently
deployed revision. The tracked production checkout must be clean and the
queue idle. The deployment backs up the queue database, validates a detached
candidate with unit tests and smoke tests, stops previously active services,
then rechecks the queue before switching source. An unexpected exit after
stopping services restores the previous release and restarts those services.
The deployment status records the exact commit and backup path. Normal
GitHub deployments retain their validated `origin/main` ancestry check.
