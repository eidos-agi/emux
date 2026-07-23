#!/usr/bin/env bash
# Fleetkick server: bridge (tab control) + ttyd (terminal). Usage: ./serve.sh [workdir]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
command -v ttyd >/dev/null || { echo "ttyd missing: brew install ttyd" >&2; exit 1; }
command -v node >/dev/null || { echo "node missing" >&2; exit 1; }

# Kill by port, not by name: a bridge started as plain `node bridge.js` doesn't match a
# path pattern, and a stale one silently keeps serving old code while the new one dies
# on EADDRINUSE.
lsof -ti tcp:7682 | xargs kill -9 2>/dev/null || true
node "$SCRIPT_DIR/bridge.js" &
trap 'kill $! 2>/dev/null' EXIT

# -O rejects websockets from foreign origins (a random webpage can't spawn claude);
# -a lets the panel pass the tabId; -W makes the tty writable.
# disableLeaveAlert: never show "Leave site?" — the panel is a UI, not a document.
# reconnect=1: on any drop, retry after a second instead of parking on "Press Enter to
# Reconnect". tmux holds the session, so a reconnect lands back in the same conversation.
exec ttyd -p 7681 -i 127.0.0.1 -O -a -W \
  -t fontSize=13 -t disableLeaveAlert=true -t disableReconnect=false -t reconnect=1 \
  -w "${1:-$PWD}" "$SCRIPT_DIR/boot.sh"
