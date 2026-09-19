#!/bin/bash
# Builds "Peak Download Manager.app" and installs it into /Applications.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
APP="$BUILD/Peak Download Manager.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "→ Compiling"
swiftc -O -target arm64-apple-macos13.0 "$ROOT/mac/PeakApp.swift" -o "$BUILD/Peak-arm64"
swiftc -O -target x86_64-apple-macos13.0 "$ROOT/mac/PeakApp.swift" -o "$BUILD/Peak-x86_64"
lipo -create "$BUILD/Peak-arm64" "$BUILD/Peak-x86_64" -output "$APP/Contents/MacOS/Peak"
rm "$BUILD"/Peak-*
cp "$ROOT/mac/Info.plist" "$APP/Contents/Info.plist"

echo "→ Icon"
ICONSET="$BUILD/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
cp "$ROOT/design/logo/icon1024.png" "$BUILD/icon1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/icon1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

cp "$ROOT/mac/menubar.png" "$ROOT/mac/menubar@2x.png" "$APP/Contents/Resources/"

echo "→ Backend"
rsync -a --exclude __pycache__ "$ROOT/backend" "$APP/Contents/Resources/"
rsync -a "$ROOT/extension" "$APP/Contents/Resources/backend/"

codesign --force --deep -s - "$APP"

echo "→ Installing to /Applications"
osascript -e 'quit app "Peak Download Manager"' 2>/dev/null || true
sleep 1
rm -rf "/Applications/Peak Download Manager.app"
cp -R "$APP" /Applications/
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "/Applications/Peak Download Manager.app"
echo "✓ Built. Open it from Launchpad or: open -a 'Peak Download Manager'"
