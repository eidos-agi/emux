#!/usr/bin/env bash
# Spawned by ttyd per Fleetkick connection; URL args land here as $1=tabId $2=windowId.
# One warm tmux session per tab: switching tabs just detaches this client — the claude
# session stays alive and reattaches instantly when its tab is active again.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# tmux joins its command args with spaces through sh -c, so the long prompt can't ride
# through new-session directly — re-enter this script with --inner instead.
if [ "${1:-}" = "--inner" ]; then
  tab="${2:-}"; win="${3:-}"
  MCP_CFG="/tmp/fleetkick-mcp-$tab.json"
  printf '{"mcpServers":{"fleetkick":{"command":"node","args":["%s/mcp.js"],"env":{"FLEETKICK_TAB":"%s"}}}}' \
    "$SCRIPT_DIR" "$tab" > "$MCP_CFG"
  exec claude --mcp-config "$MCP_CFG" --append-system-prompt "You are Fleetkick — a Claude Code session in a Chrome side panel, in a per-tab tmux session. Your paired browser tab: tabId=$tab${win:+, windowId=$win}. See AND control the browser ONLY with the fleetkick MCP tools (read, click, type, navigate, screenshot, tab_create, tabs_list) — never claude-in-chrome. They default to your paired tab; pass tabId to steer other tabs you create. For tmux sessions, the fleet, or anything terminal-side, use the emux MCP tools."
fi

if [ -z "${1:-}" ]; then
  exec claude
fi
exec tmux new-session -A -s "fleetkick-tab-$1" "$SCRIPT_DIR/boot.sh" --inner "$1" "${2:-}"
# ponytail: fleetkick-tab-* sessions accumulate as tabs churn; reap on tab-close (or emux
# supervision) once this loop is proven.
