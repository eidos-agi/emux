#!/usr/bin/env bash
# Spawned by ttyd per Fleetkick connection; URL args land here as
#   $1=installId (8 hex, identifies the browser profile) $2=tabId $3=windowId
# One warm tmux session per (install, tab): switching tabs just detaches this client — the
# claude session stays alive and reattaches instantly when its tab is active again.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# A missing `claude` used to be invisible: exec fails, the session's command exits, tmux
# drops the session, and /switch still answered ok. Hold the pane open with the reason on
# screen instead — a session you can read the error in beats one that never appears.
# ponytail: sleep, not a retry loop; the fix is always PATH, and the pane says so.
command -v claude >/dev/null || {
  echo "fleetkick: 'claude' not found on PATH" >&2
  echo "  PATH=$PATH" >&2
  echo "  fix: add its dir to EnvironmentVariables/PATH in com.eidos.fleetkick.plist," >&2
  echo "       then re-run ./install-daemon.sh" >&2
  exec sleep 86400
}

# Args arrive from a URL. tmux runs its command through sh -c, and these values become a
# /tmp path, a session name and a JSON string, so anything outside the expected shape is an
# injection. Validate positionally — the install id is hex, everything else is digits.
ok_install() {
  case "$1" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) return 0 ;;
    *) echo "fleetkick: bad install id '$1' (8 hex chars)" >&2; return 1 ;;
  esac
}
ok_num() {
  case "$1" in
    ''|*[!0-9]*) echo "fleetkick: bad numeric arg '$1'" >&2; return 1 ;;
    *) return 0 ;;
  esac
}

# A pane in an existing session: same tab, same browser, but a role. The manager and the
# worker see identical tab data — the only difference is who they are and who they answer
# to, which is what makes the pair useful rather than two agents guessing at each other.
if [ "${1:-}" = "--pane" ]; then
  install="${2:-}"; tab="${3:-}"; role="${4:-worker}"; agent="${5:-claude}"; manager="${6:-}"
  ok_install "$install" || exit 1
  ok_num "$tab" || exit 1
  case "$role" in manager|worker) ;; *) echo "fleetkick: bad role '$role'" >&2; exit 1 ;; esac
  case "$agent" in claude|grok|codex|gemini) ;; *) echo "fleetkick: bad agent '$agent'" >&2; exit 1 ;; esac
  command -v "$agent" >/dev/null || { echo "fleetkick: '$agent' is not installed" >&2; exec sleep 86400; }

  export FLEETKICK_INSTALL="$install" FLEETKICK_ROLE="$role" FLEETKICK_MANAGER="$manager"
  export FLEETKICK_SESSION_TAB="$tab"
  export FLEETKICK_PANE="${TMUX_PANE:-}"

  if [ "$role" = "manager" ]; then
    WHO="You are the MANAGER of this Fleetkick pane group. You do not do the work yourself: you decide what should happen, then direct your worker. Your worker runs in another pane of this same tmux session; use the fleetkick tools panes (to see it) and send_to_pane (to give it instructions, one clear task at a time). Read the browser yourself to decide what to ask for, then verify the worker's result."
  else
    WHO="You are a WORKER in this Fleetkick pane group. Your manager is pane ${manager:-unknown} and instructions will arrive as typed input from it. Do the task you are given, report the outcome plainly, and do not spawn further panes unless asked."
  fi
  RULES="$WHO You and every other pane in this group are looking at the SAME browser tab (tabId=$tab, browser install $install), so tab data is shared context, not something to re-establish. Control the browser ONLY with the fleetkick MCP tools (read, click, type, navigate, screenshot, tab_create, tabs_list, panes, send_to_pane) — never claude-in-chrome. Your tools are bound to this ONE browser profile; several browsers can share this daemon and you can only see your own."

  MCP_CFG="/tmp/fleetkick-mcp-$install-$tab.json"
  printf '{"mcpServers":{"fleetkick":{"command":"node","args":["%s/mcp.js"],"env":{"FLEETKICK_INSTALL":"%s"}}}}' \
    "$SCRIPT_DIR" "$install" > "$MCP_CFG"

  case "$agent" in
    claude) exec claude --mcp-config "$MCP_CFG" --append-system-prompt "$RULES" ;;
    grok)
      # grok has no per-invocation MCP config; it reads ~/.grok/config.toml. `mcp add` is
      # an upsert, so running it every time is idempotent and keeps the path correct.
      # The server inherits this pane's env, which is how FLEETKICK_INSTALL reaches it.
      grok mcp add fleetkick --scope user node "$SCRIPT_DIR/mcp.js" >/dev/null 2>&1 || true
      exec grok --rules "$RULES"
      ;;
    # ponytail: codex/gemini get the env but no prompt injection — neither is in the demo
    # path yet, and guessing at their prompt flags would be untested code.
    *) exec "$agent" ;;
  esac
fi

# tmux joins its command args with spaces through sh -c, so the long prompt can't ride
# through new-session directly — re-enter this script with --inner instead.
if [ "${1:-}" = "--inner" ]; then
  install="${2:-}"; tab="${3:-}"; win="${4:-}"
  ok_install "$install" || exit 1
  ok_num "$tab" || exit 1
  MCP_CFG="/tmp/fleetkick-mcp-$install-$tab.json"
  # FLEETKICK_INSTALL pins the tools to THIS browser. Without it the bridge would have to
  # guess which browser a command meant, and with several open it would guess wrong.
  # Still no FLEETKICK_TAB: within the browser, tools follow the live selection.
  printf '{"mcpServers":{"fleetkick":{"command":"node","args":["%s/mcp.js"],"env":{"FLEETKICK_INSTALL":"%s"}}}}' \
    "$SCRIPT_DIR" "$install" > "$MCP_CFG"
  export FLEETKICK_INSTALL="$install"
  export FLEETKICK_SESSION_TAB="$tab"
  exec claude --mcp-config "$MCP_CFG" --append-system-prompt "You are Fleetkick — a Claude Code session in a browser side panel, in a per-tab tmux session (this session started on tabId=$tab${win:+, windowId=$win}, in browser install $install). See AND control the browser ONLY with the fleetkick MCP tools (read, click, type, navigate, screenshot, tab_create, tabs_list) — never claude-in-chrome. Your tools are bound to ONE browser profile: several Chromium browsers can share this daemon, and you can only see and act on the tabs of your own. Within it you are attached to EVERY tab in every window at once: with no tabId, a tool acts on the SELECTED tab — whichever one the human is on at that moment, which changes as they switch tabs. Call tabs_list to enumerate all of them (id, window, title, url, active, pinned) and pass a tabId to act on a specific one instead. If it matters which tab you touch, read tabs_list or pass tabId explicitly rather than assuming the selection hasn't moved. For tmux sessions, the fleet, or anything terminal-side, use the emux MCP tools."
fi

if [ -z "${1:-}" ]; then
  exec claude
fi

install="$1"; tab="${2:-}"; win="${3:-}"
ok_install "$install" || exit 1
ok_num "$tab" || exit 1
# Re-attach forever instead of exec'ing once. A tmux client exits cleanly whenever it
# detaches — another client taking the session, a kill-session, a switch-client — and that
# clean exit ends the ttyd process, which is what parks the terminal on "Press Enter to
# Reconnect". ttyd's reconnect only covers a DROPPED socket, not a process that finished,
# so no client setting can fix this; the server has to refuse to end. tmux holds the
# session either way, so re-attaching is seamless rather than a new conversation.
fails=0
while :; do
  if tmux new-session -A -s "fleetkick-$install-$tab" \
       "$SCRIPT_DIR/boot.sh" --inner "$install" "$tab" "$win"; then
    fails=0
  else
    fails=$((fails + 1))
  fi
  # Don't spin: a genuinely broken attach would otherwise burn a core silently.
  if [ "$fails" -ge 5 ]; then
    echo "fleetkick: tmux attach failed 5 times in a row — giving up rather than spinning" >&2
    exec sleep 86400
  fi
  sleep 0.3
done
# ponytail: fleetkick-* sessions accumulate as tabs churn; reap on tab-close (or emux
# supervision) once this loop is proven.
