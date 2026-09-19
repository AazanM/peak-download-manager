#!/bin/bash
PLIST="$HOME/Library/LaunchAgents/com.peakdm.backend.plist"
launchctl unload "$PLIST" 2>/dev/null; rm -f "$PLIST"; echo "Peak Download Manager backend removed."
