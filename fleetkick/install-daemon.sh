#!/usr/bin/env bash
# Install the Fleetkick daemon as a LaunchAgent so it restarts on crash, login, and
# reboot. Uninstall: launchctl bootout gui/$UID/com.eidos.fleetkick && rm the plist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
WORKDIR="${1:-$(cd "$DIR/.." && pwd)}"
LABEL="com.eidos.fleetkick"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__DIR__|$DIR|g" -e "s|__WORKDIR__|$WORKDIR|g" "$DIR/$LABEL.plist" > "$DEST"

# bootout is the documented way to replace a running agent; it fails when nothing is
# loaded, which is a normal first install, not an error.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
# Any hand-started copy would hold the ports and make the agent crash-loop on bind.
lsof -ti tcp:7682 | xargs kill -9 2>/dev/null || true
lsof -ti tcp:7681 | xargs kill -9 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID" "$DEST"

echo "loaded $LABEL -> $DEST"
echo "workdir: $WORKDIR"
