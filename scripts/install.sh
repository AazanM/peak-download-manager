#!/bin/bash
# Installs deps and makes the Peak Download Manager backend start automatically at login.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
brew list yt-dlp >/dev/null 2>&1 || brew install yt-dlp
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
# Remove the old "Grabber" service if it exists.
OLD="$HOME/Library/LaunchAgents/com.grabber.backend.plist"
[ -f "$OLD" ] && { launchctl unload "$OLD" 2>/dev/null || true; rm -f "$OLD"; }
PLIST="$HOME/Library/LaunchAgents/com.peakdm.backend.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.peakdm.backend</string>
  <key>ProgramArguments</key><array><string>$(which python3)</string><string>$DIR/backend/server.py</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/peak-dm.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Peak Download Manager running at http://127.0.0.1:7878 (starts at login)."
