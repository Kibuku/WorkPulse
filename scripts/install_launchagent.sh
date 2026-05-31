#!/usr/bin/env bash
# install_launchagent.sh — register WorkPulse as a macOS LaunchAgent so the
# tray launches at every login. This is the Mac equivalent of the Windows
# Startup-folder shortcut.
#
# Re-runnable. Idempotent — replaces the existing agent if one is registered.

set -euo pipefail

ROOT="$( cd -- "$(dirname "${BASH_SOURCE[0]}")/.." &> /dev/null && pwd )"
PLIST_DIR="$HOME/Library/LaunchAgents"
LABEL="com.workpulse.tray"
PLIST="$PLIST_DIR/$LABEL.plist"
PYTHON="$ROOT/.venv/bin/python"
TRAY="$ROOT/scripts/tray.py"
LOG_DIR="$ROOT/logs"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: $PYTHON not found." >&2
    echo "Run setup.sh first to create the .venv and install dependencies." >&2
    exit 1
fi
if [ ! -f "$TRAY" ]; then
    echo "ERROR: $TRAY not found." >&2
    exit 1
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$TRAY</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <!-- Launch automatically at login -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Don't restart automatically if the user quits the tray manually -->
    <key>KeepAlive</key>
    <false/>

    <!-- Interactive so the menu-bar item is allowed -->
    <key>ProcessType</key>
    <string>Interactive</string>

    <!-- Send stdout/stderr to our logs folder for debugging -->
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchagent.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchagent.err.log</string>
</dict>
</plist>
EOF

# Unload any existing copy, then load fresh
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "  Installed: $PLIST"
echo "  Label:     $LABEL"
echo
echo "  Useful commands:"
echo "    launchctl list | grep workpulse        # confirm it's loaded"
echo "    launchctl start  $LABEL                # start now"
echo "    launchctl stop   $LABEL                # stop now"
echo "    launchctl unload $PLIST                # disable autostart"
echo "    tail -f $LOG_DIR/launchagent.err.log   # watch for startup errors"
