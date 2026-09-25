#!/bin/sh
# Start the eve-threads sidecar automatically at login (macOS), so you never need a
# terminal. launchd also restarts it if it stops. Log: sidecar/sidecar.log
#   Install:   ./install_autostart.sh
#   Uninstall: ./install_autostart.sh --uninstall
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.evethreads.sidecar
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed: the sidecar no longer starts at login."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/run.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/sidecar.log</string>
  <key>StandardErrorPath</key><string>$DIR/sidecar.log</string>
</dict>
</plist>
EOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed: the sidecar now starts at login. Log: $DIR/sidecar.log"
