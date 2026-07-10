#!/bin/bash
# scripts/setup-desktop-agent.sh
# Helps Mac users set up the Conveyor desktop observe agent.
# It does NOT build the helper (that must be done from capture-your-screen).

set -e

echo "=== Conveyor Desktop Agent Setup ==="
echo "This helps set up the polling agent on macOS."
echo "Note: You still need to build 'capture-screen-helper' separately."
echo ""

# Check macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "This script is for macOS only."
    exit 1
fi

# 1. Find or prompt for the helper
HELPER_DEFAULT="/usr/local/bin/capture-screen-helper"
if [ -x "$HELPER_DEFAULT" ]; then
    HELPER_PATH="$HELPER_DEFAULT"
else
    echo "capture-screen-helper not found at $HELPER_DEFAULT"
    read -rp "Enter full path to capture-screen-helper binary: " HELPER_PATH
fi

if [ ! -x "$HELPER_PATH" ]; then
    echo "Error: $HELPER_PATH is not executable."
    echo "Please build it from the capture-your-screen repo first:"
    echo "  git clone <the-repo-url>"
    echo "  cd capture-your-screen && bash scripts/build_helper.sh"
    echo "  sudo cp build/Release/capture-screen-helper /usr/local/bin/"
    exit 1
fi

echo "Using helper: $HELPER_PATH"

# 2. Conveyor dir
CONVEYOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "Conveyor dir: $CONVEYOR_DIR"

# 3. Prompt for key env
echo ""
echo "Please provide the following (you can also edit .env or your shell profile later):"
read -rp "CONVEYOR_CONTROL_PLANE_URL (e.g. http://your-vps:8766 or via SSH tunnel 127.0.0.1:8766): " CONTROL_PLANE
read -rp "CONVEYOR_DESKTOP_AGENT_TOKEN: " AGENT_TOKEN
read -rp "CONVEYOR_DESKTOP_NODE_ID (default macbook-payton): " NODE_ID
NODE_ID=${NODE_ID:-macbook-payton}
read -rp "CONVEYOR_DESKTOP_NODE_NAME (default Payton MacBook): " NODE_NAME
NODE_NAME=${NODE_NAME:-"Payton MacBook"}

read -rp "SSH host for tunnel (default vps-oracle, must be in ~/.ssh/config): " SSH_HOST_IN
SSH_HOST_IN=${SSH_HOST_IN:-vps-oracle}
read -rp "Local tunnel port (default 8766): " LOCAL_PORT_IN
LOCAL_PORT_IN=${LOCAL_PORT_IN:-8766}

# 4. Write a helper env file
ENV_FILE="$CONVEYOR_DIR/.desktop-agent.env"
cat > "$ENV_FILE" << EOF
export CONVEYOR_CONTROL_PLANE_URL="$CONTROL_PLANE"
export CONVEYOR_DESKTOP_AGENT_TOKEN="$AGENT_TOKEN"
export CONVEYOR_DESKTOP_NODE_ENABLED=true
export CONVEYOR_DESKTOP_NODE_ID="$NODE_ID"
export CONVEYOR_DESKTOP_NODE_NAME="$NODE_NAME"
export CONVEYOR_DESKTOP_SCREENSHOT_HELPER="$HELPER_PATH"
export CONVEYOR_DESKTOP_SCREENSHOT_DIR="$(cd ~ && pwd)/.codex/desktop/screenshots"
export CONVEYOR_DESKTOP_UPLOAD_ENABLED=true
export CONVEYOR_DESKTOP_AUTO_THUMBNAIL_ON_OBSERVE=true
EOF

echo ""
echo "Created $ENV_FILE"

# 5. Create a simple run script
RUN_SCRIPT="$CONVEYOR_DIR/run-desktop-agent.sh"
cat > "$RUN_SCRIPT" << 'EOF'
#!/bin/bash
set -a
source "$(dirname "$0")/.desktop-agent.env"
set +a
cd "$(dirname "$0")"
.venv/bin/python desktop_agent.py --poll-observe --poll-computer
EOF
chmod +x "$RUN_SCRIPT"

echo "Created $RUN_SCRIPT"

# 6. Optional: launchd plist for the SSH tunnel (auto start + auto reconnect).
# The desktop agent itself is supervised by the ConveyorAgent menu bar app,
# not launchd — macOS 26 blocks launchd-spawned CLI binaries from ~/Documents.
echo ""
read -rp "Create the SSH-tunnel launch agent (auto start/reconnect at login)? (y/N) " CREATE_LAUNCH
if [[ "$CREATE_LAUNCH" =~ ^[Yy]$ ]]; then
    export CONVEYOR_DIR CONVEYOR_SSH_HOST="$SSH_HOST_IN" CONVEYOR_LOCAL_PORT="$LOCAL_PORT_IN"
    "$CONVEYOR_DIR/scripts/install-launchagents.sh"
    echo ""
    echo "SSH tunnel launch agent installed: ~/Library/LaunchAgents/com.conveyor.ssh-tunnel.plist"
    echo "It starts at login and auto-restarts on disconnect."
    echo ""
    echo "Desktop agent: built and run via ConveyorAgent.app (menubar-agent/)."
    echo "  bash menubar-agent/build.sh install"
    echo "  open '/Applications/Conveyor Agent.app'"
    echo "  Then grant Full Disk Access + Screen Recording to Conveyor Agent.app"
    echo "  in System Settings > Privacy & Security. The app auto-starts the agent."
    echo "Manage tunnel with: $CONVEYOR_DIR/scripts/install-launchagents.sh --status|--uninstall"
fi

echo ""
echo "=== Next steps ==="
echo "1. SSH tunnel: handled by launchd (if you said y above)."
echo "2. Desktop agent: install + run the menu bar app (see above)."
echo "   Screen Recording: bash scripts/grant-screen-recording.sh"
echo "   or menu bar → 开启屏幕录制权限…"
echo "   Grant Full Disk Access to Conveyor Agent.app if repo is under ~/Documents."
echo "3. On VPS, make sure desktop_agent_server.py is running if not using systemd."
echo ""
echo "For the full one-click feel on VPS: use scripts/install-remote.sh from your laptop."
