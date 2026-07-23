#!/usr/bin/env bash
# Serve claude code for the Chrome sidebar extension. Usage: ./serve.sh [workdir]
set -euo pipefail
command -v ttyd >/dev/null || { echo "ttyd missing: brew install ttyd" >&2; exit 1; }
# -O rejects websocket connections from foreign origins (random webpages can't spawn claude);
# -a lets the extension pass --append-system-prompt with the tab info; -W makes the tty writable.
exec ttyd -p 7681 -i 127.0.0.1 -O -a -W -t fontSize=13 -w "${1:-$PWD}" claude
