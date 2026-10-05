#!/usr/bin/env bash
# scripts/setup-vps-uinput.sh
# Configure uinput, libinput, udev rules, and xrdp xorg.conf on the VPS
# so cua-driver can create kernel virtual input devices and emit real mouse events.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (or via sudo)." >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-ubuntu}"

echo "=== 1. Installing required packages ==="
apt-get update -qq
apt-get install -y -qq \
    xserver-xorg-input-libinput \
    xserver-xorg-input-evdev \
    python3-gi-cairo

echo "=== 2. Ensuring uinput kernel module is loaded ==="
modprobe uinput || true
mkdir -p /etc/modules-load.d
echo "uinput" > /etc/modules-load.d/uinput.conf

echo "=== 3. Writing udev rules for uinput and CUA virtual devices ==="
cat > /etc/udev/rules.d/99-uinput.rules <<'EOF'
KERNEL=="uinput", MODE="0666", GROUP="input"
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="CUA*", MODE="0666", GROUP="input"
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*uinput*", MODE="0666", GROUP="input"
EOF

echo "=== 4. Adding user '${TARGET_USER}' to 'input' group ==="
usermod -a -G input "${TARGET_USER}"

echo "=== 5. Reloading udev rules ==="
udevadm control --reload-rules || true
udevadm trigger || true

echo "=== 6. Updating xrdp xorg.conf for AutoAddDevices ==="
XRDP_XORG_CONF="/etc/X11/xrdp/xorg.conf"
if [[ -f "${XRDP_XORG_CONF}" ]]; then
    if grep -q "AutoAddDevices" "${XRDP_XORG_CONF}"; then
        sed -i 's/Option *"AutoAddDevices" *"off"/Option "AutoAddDevices" "on"/' "${XRDP_XORG_CONF}"
    else
        sed -i '/Section *"ServerFlags"/a \    Option "AutoAddDevices" "on"' "${XRDP_XORG_CONF}"
    fi
    echo "Updated ${XRDP_XORG_CONF} to ensure AutoAddDevices is 'on'."
else
    echo "Note: ${XRDP_XORG_CONF} not found; skipping xrdp xorg edit."
fi

echo "=== 7. Verifying /dev/uinput permissions ==="
ls -la /dev/uinput

echo "=== Setup complete ==="
echo "If xrdp is running, you may need to restart the session or run: systemctl restart xrdp"
