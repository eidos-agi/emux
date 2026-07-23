#!/usr/bin/env bash
# Fleetkick server: bridge (tab control) + ttyd (terminal). Usage: ./serve.sh [workdir]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
command -v ttyd >/dev/null || { echo "ttyd missing: brew install ttyd" >&2; exit 1; }
command -v node >/dev/null || { echo "node missing" >&2; exit 1; }

pkill -f "fleetkick/bridge.js" 2>/dev/null || true
node "$SCRIPT_DIR/bridge.js" &
trap 'kill $! 2>/dev/null' EXIT

# -O rejects websockets from foreign origins (a random webpage can't spawn claude);
# -a lets the panel pass the tabId; -W makes the tty writable.
exec ttyd -p 7681 -i 127.0.0.1 -O -a -W -t fontSize=13 -w "${1:-$PWD}" "$SCRIPT_DIR/boot.sh"
