#!/usr/bin/env bash
# menubar-agent/build.sh
#
# Build the Conveyor menu bar agent and package it as a macOS .app bundle
# with LSUIElement=true (no Dock icon, menu bar only).
#
# Output: build/ConveyorAgent.app
#
# Usage:
#   bash menubar-agent/build.sh          # debug build
#   bash menubar-agent/build.sh release  # optimized build
#   bash menubar-agent/build.sh install  # build release + copy to /Applications
set -euo pipefail

CONFIG="${1:-debug}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ConveyorAgent"
APP_BUNDLE="$HERE/build/$APP_NAME.app"

case "$CONFIG" in
    release|install) SWIFT_CONFIG="-c release" ;;
    *)               SWIFT_CONFIG="" ;;
esac

log() { echo "[build] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "menu bar agent is macOS only."

log "swift build $SWIFT_CONFIG"
( cd "$HERE" && swift build $SWIFT_CONFIG )

# Locate the built binary.
BIN_PATH="$(cd "$HERE" && swift build $SWIFT_CONFIG --show-bin-path)/$APP_NAME"
[[ -x "$BIN_PATH" ]] || die "binary not found at $BIN_PATH"
log "binary: $BIN_PATH"

log "assembling bundle -> $APP_BUNDLE"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/$APP_NAME"
cp "$HERE/Resources/Info.plist" "$APP_BUNDLE/Contents/Info.plist"

# Touch the bundle so Finder/LaunchServices picks up metadata.
touch "$APP_BUNDLE"

# Ad-hoc sign the bundle. macOS won't show notification authorization prompts
# (and won't reliably offer Login Items / TCC grants) for unsigned apps.
codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null \
    && log "ad-hoc signed: $APP_BUNDLE" \
    || log "WARNING: codesign failed (notifications/TCC may not work)"

log "built: $APP_BUNDLE"

if [[ "$CONFIG" == "install" ]]; then
    DEST="/Applications/Conveyor Agent.app"
    log "installing -> $DEST"
    rm -rf "$DEST"
    cp -R "$APP_BUNDLE" "$DEST"
    # Re-sign the installed copy (copying can invalidate the signature seal).
    codesign --force --deep --sign - "$DEST" 2>/dev/null \
        && log "ad-hoc signed: $DEST" \
        || log "WARNING: codesign of $DEST failed"
    # Refresh LaunchServices so the app is recognized.
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$DEST" 2>/dev/null || true
    log "installed. open with: open '$DEST'"
    log "to launch at login: System Settings > General -> Login Items -> add Conveyor Agent.app"
fi
