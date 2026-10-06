#!/bin/bash
# Run the host desktop (X server + desktop session) under systemd, so it is
# there after every boot without anyone logging in over RDP.
#
#   CONVEYOR_HOST_DISPLAY        display number (default 9, below xrdp's range)
#   CONVEYOR_HOST_DESKTOP_SIZE   WIDTHxHEIGHT (default 1600x900; a mode from
#                                /etc/X11/conveyor/xorg.conf)
#
# The script stays in the foreground for as long as the session lives and
# exits non-zero when it ends, so systemd brings the desktop back.
set -euo pipefail

display=":${CONVEYOR_HOST_DISPLAY:-9}"
size="${CONVEYOR_HOST_DESKTOP_SIZE:-1600x900}"
xorg="/usr/lib/xorg/Xorg"
config="conveyor/xorg.conf"   # relative: a non-root X server only reads configs under /etc/X11

[[ -x "${xorg}" ]] || { echo "Xorg not found at ${xorg}" >&2; exit 1; }
[[ -r "/etc/X11/${config}" ]] || { echo "/etc/X11/${config} is missing (install deploy/host-desktop-xorg.conf)" >&2; exit 1; }

cd "${HOME}"

# A desktop session of this user that someone else started (an RDP login) is
# the host desktop for now. Starting a second one would leave two desktops
# and no way to tell which one the Agent is looking at.
while pgrep -u "$(id -u)" -x xfce4-session >/dev/null 2>&1; do
  echo "A desktop session is already running; waiting for it to end."
  sleep 20
done

export XAUTHORITY="${HOME}/.Xauthority"
xauth -f "${XAUTHORITY}" remove "${display}" >/dev/null 2>&1 || true
xauth -f "${XAUTHORITY}" add "${display}" MIT-MAGIC-COOKIE-1 "$(mcookie)"
rm -f "/tmp/.X${display#:}-lock"

"${xorg}" "${display}" -auth "${XAUTHORITY}" -config "${config}" \
  -noreset -nolisten tcp -logfile ".xorg-conveyor.${display#:}.log" &
xorg_pid=$!
cleanup() { kill "${xorg_pid}" 2>/dev/null || true; }
trap cleanup EXIT

export DISPLAY="${display}"
for _ in $(seq 1 50); do
  xdpyinfo >/dev/null 2>&1 && break
  kill -0 "${xorg_pid}" 2>/dev/null || { echo "X server exited during start" >&2; exit 1; }
  sleep 0.2
done
xdpyinfo >/dev/null 2>&1 || { echo "X server did not come up on ${display}" >&2; exit 1; }
xrandr -s "${size}" >/dev/null 2>&1 || echo "Could not set ${size}; keeping the default size." >&2
echo "Host desktop up on ${display} ($(xdotool getdisplaygeometry 2>/dev/null | tr ' ' 'x'))"

# Same session start as an RDP login gets, so the desktop looks and behaves
# the same; a private session bus is created for it.
unset DBUS_SESSION_BUS_ADDRESS
export DESKTOP_SESSION=xfce XDG_SESSION_DESKTOP=xfce XDG_CURRENT_DESKTOP=XFCE
if [[ -x "${HOME}/.xsession" ]]; then
  dbus-launch --exit-with-session "${HOME}/.xsession" || true
elif [[ -r "${HOME}/.xsession" ]]; then
  dbus-launch --exit-with-session sh "${HOME}/.xsession" || true
else
  dbus-launch --exit-with-session xfce4-session || true
fi
echo "Desktop session ended." >&2
exit 1
