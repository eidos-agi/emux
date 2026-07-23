#!/usr/bin/env bash
# Spawned by ttyd per Fleetkick connection; URL args land here as $1=tabId $2=windowId.
# One warm tmux session per tab: switching tabs just detaches this client — the claude
# session stays alive and reattaches instantly when its tab is active again.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Args arrive from a URL. tmux runs its command through sh -c and $tab becomes a /tmp
# path and a JSON string, so anything but digits is an injection. Reject it here, once.
for a in "$@"; do
  case "$a" in --inner|'') ;; *[!0-9]*) echo "fleetkick: bad arg '$a' (digits only)" >&2; exit 1 ;; esac
done

# tmux joins its command args with spaces through sh -c, so the long prompt can't ride
# through new-session directly — re-enter this script with --inner instead.
if [ "${1:-}" = "--inner" ]; then
  tab="${2:-}"; win="${3:-}"
  MCP_CFG="/tmp/fleetkick-mcp-$tab.json"
  # No FLEETKICK_TAB: the tools follow the live selection instead of pinning to $tab.
  printf '{"mcpServers":{"fleetkick":{"command":"node","args":["%s/mcp.js"]}}}' \
    "$SCRIPT_DIR" > "$MCP_CFG"
  exec claude --mcp-config "$MCP_CFG" --append-system-prompt "You are Fleetkick — a Claude Code session in a Chrome side panel, in a per-tab tmux session (this session started on tabId=$tab${win:+, windowId=$win}). See AND control the browser ONLY with the fleetkick MCP tools (read, click, type, navigate, screenshot, tab_create, tabs_list) — never claude-in-chrome. You are attached to EVERY tab in every window at once: with no tabId, a tool acts on the SELECTED tab — whichever one the human is on at that moment, which changes as they switch tabs. Call tabs_list to enumerate all of them (id, window, title, url, active, pinned) and pass a tabId to act on a specific one instead. If it matters which tab you touch, read tabs_list or pass tabId explicitly rather than assuming the selection hasn't moved. For tmux sessions, the fleet, or anything terminal-side, use the emux MCP tools."
fi

if [ -z "${1:-}" ]; then
  exec claude
fi
exec tmux new-session -A -s "fleetkick-tab-$1" "$SCRIPT_DIR/boot.sh" --inner "$1" "${2:-}"
# ponytail: fleetkick-tab-* sessions accumulate as tabs churn; reap on tab-close (or emux
# supervision) once this loop is proven.
