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

# Resolve a stable code-signing identity (preferred over ad-hoc). Ad-hoc signed
# apps get a placeholder icon in notification banners because usernoted won't
# resolve the bundle icon without a persistent identity; a real (self-signed or
# Apple Development) cert fixes that.
resolve_sign_identity() {
    local id
    id="$(security find-identity -p codesigning -v 2>/dev/null \
          | grep -oE '[A-F0-9]{40} "[^"]+"' | head -1 | sed -E 's/^[A-F0-9]+ "//; s/"$//')"
    if [[ -n "$id" ]]; then
        echo "$id"
    else
        echo "-"
    fi
}
SIGN_IDENTITY="$(resolve_sign_identity)"
[[ "$SIGN_IDENTITY" != "-" ]] \
    && log "sign identity: $SIGN_IDENTITY" \
    || log "sign identity: ad-hoc (no codesigning cert found; notification icon may be placeholder)"

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

# Stamp a fresh build number so LaunchServices treats each build as new and
# re-reads the bundle's icon (a static CFBundleVersion makes LS cache the
# icon forever, which is why notification banners showed a placeholder).
BUILD_NUM="$(date +%Y%m%d%H%M%S)"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUM" \
    "$APP_BUNDLE/Contents/Info.plist" 2>/dev/null \
    && log "stamped CFBundleVersion=$BUILD_NUM" \
    || log "WARNING: could not stamp CFBundleVersion"

# Copy menu-bar icon assets + app icon into Contents/Resources.
# Bundle.main.image(forResource:) finds flat PNGs at the root of Resources,
# so we flatten the per-state icons and drop AppIcon.icns next to them.
ASSETS_DIR="$HERE/Resources/Assets"
if [[ -d "$ASSETS_DIR/MenuBar" ]]; then
    cp "$ASSETS_DIR/MenuBar"/*.png "$APP_BUNDLE/Contents/Resources/" \
        && log "copied $(ls "$ASSETS_DIR/MenuBar"/*.png | wc -l | tr -d ' ') menu-bar icons" \
        || die "failed to copy menu-bar icons"
fi
if [[ -f "$ASSETS_DIR/AppIcon/AppIcon.icns" ]]; then
    cp "$ASSETS_DIR/AppIcon/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/" \
        && log "copied AppIcon.icns" \
        || die "failed to copy AppIcon.icns"
fi

# Touch the bundle so Finder/LaunchServices picks up metadata.
touch "$APP_BUNDLE"

# Ad-hoc sign the bundle. macOS won't show notification authorization prompts
# (and won't reliably offer Login Items / TCC grants) for unsigned apps.
codesign --force --deep --sign "$SIGN_IDENTITY" "$APP_BUNDLE" 2>/dev/null \
    && log "signed: $APP_BUNDLE ($SIGN_IDENTITY)" \
    || log "WARNING: codesign failed (notifications/TCC may not work)"

log "built: $APP_BUNDLE"

if [[ "$CONFIG" == "install" ]]; then
    DEST="/Applications/Conveyor Agent.app"
    log "installing -> $DEST"
    rm -rf "$DEST"
    cp -R "$APP_BUNDLE" "$DEST"
    # Re-sign the installed copy (copying can invalidate the signature seal).
    codesign --force --deep --sign "$SIGN_IDENTITY" "$DEST" 2>/dev/null \
        && log "signed: $DEST ($SIGN_IDENTITY)" \
        || log "WARNING: codesign of $DEST failed"
    # Refresh LaunchServices so the app is recognized. -f forces re-registration
    # even if the bundle path/ID is already known; -r recurses into the bundle so
    # the icon and document bindings are picked up.
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f -r "$DEST" 2>/dev/null || true
    log "installed. open with: open '$DEST'"
    log "to launch at login: System Settings > General -> Login Items -> add Conveyor Agent.app"
fi
