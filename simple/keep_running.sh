#!/bin/bash
# Keep watch_and_upload.py running in the background, even after restarts.
#   ./keep_running.sh start   - start now and at every login
#   ./keep_running.sh stop    - stop it
#   ./keep_running.sh log     - watch what it is doing
set -e
LABEL=com.studio.youtube-watcher
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/watch_and_upload.py"
LOG="$HOME/Library/Logs/youtube-watcher.log"

case "$1" in
  start)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>-u</string><string>$SCRIPT</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Running. It will start again automatically when you log in."
    echo "See what it's doing:  ./keep_running.sh log"
    ;;
  stop)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Stopped."
    ;;
  log)
    touch "$LOG"; tail -n 50 -f "$LOG"
    ;;
  *) echo "usage: $0 start | stop | log" ;;
esac
