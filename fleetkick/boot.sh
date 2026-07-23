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

# One agent per SLOT, and one tmux session per slot. Slot 0 is the tab's original session;
# slots 1+ are extra agents on the same tab. Each gets its OWN ttyd client and its own
# iframe, because the panel — not tmux — owns the layout: a divider drawn in panel HTML can
# be dragged and clicked, whereas a tmux pane border lives inside a cross-origin iframe the
# panel cannot reach at all. That was the whole reason splits felt like one crowded terminal.
if [ "${1:-}" = "--inner" ]; then
  install="${2:-}"; tab="${3:-}"; slot="${4:-0}"; role="${5:-}"; agent="${6:-}"; me="${7:-}"
  ok_install "$install" || exit 1
  ok_num "$tab" || exit 1
  ok_num "$slot" || exit 1

  sess="fleetkick-$install-$tab"
  [ "$slot" = "0" ] || sess="$sess-$slot"

  # Args win; otherwise recover from the session itself, so a reattach after a daemon
  # restart keeps its identity instead of silently reverting to a default.
  [ -n "$role" ]  || role=$(tmux show-options -v -t "$sess" @fk_role 2>/dev/null)
  [ -n "$agent" ] || agent=$(tmux show-options -v -t "$sess" @fk_agent 2>/dev/null)
  [ -n "$me" ]    || me=$(tmux show-options -v -t "$sess" @fk_name 2>/dev/null)
  # Named, not numbered: "agent0" is a slot id, not an agent. Random rather than indexed,
  # or slot 0 is called the same thing in every tab you ever open.
  [ -n "$me" ] || me=$(awk 'BEGIN{srand()} {a[NR]=$0} END{if(NR)print a[int(rand()*NR)+1]}' \
    "$SCRIPT_DIR/names.txt" 2>/dev/null)
  [ -n "$me" ] || me="agent$slot"
  [ -n "$role" ]  || role=solo
  [ -n "$agent" ] || agent=claude
  case "$role" in manager|worker|solo) ;; *) role=solo ;; esac
  case "$agent" in claude|grok|codex|gemini) ;; *) agent=claude ;; esac
  command -v "$agent" >/dev/null || {
    echo "fleetkick: '$agent' is not installed" >&2; exec sleep 86400; }

  tmux set-option -t "$sess" @fk_role "$role" 2>/dev/null
  tmux set-option -t "$sess" @fk_agent "$agent" 2>/dev/null
  tmux set-option -t "$sess" @fk_slot "$slot" 2>/dev/null
  tmux set-option -t "$sess" @fk_name "$me" 2>/dev/null

  export FLEETKICK_INSTALL="$install" FLEETKICK_SESSION_TAB="$tab"
  export FLEETKICK_SLOT="$slot" FLEETKICK_ROLE="$role" FLEETKICK_SESSION="$sess"
  export FLEETKICK_NAME="$me"

  case "$role" in
    manager) WHO="Your name is ${me}. You are the MANAGER of this Fleetkick group. You do not do the work yourself: you decide what should happen and direct your workers. Each teammate runs in its own terminal beside yours; use the fleetkick tools group (to see them, by name) and send_to_agent (to give one a task, addressed by name). Read the browser yourself to decide what to ask for, then check the result." ;;
    worker)  WHO="Your name is ${me}. You are a WORKER in this Fleetkick group. Your manager runs in another terminal beside yours and its instructions arrive as typed input. Sign anything you report back with your name so the group can tell who answered. Do the task you are given, report the outcome plainly, and do not add teammates unless asked." ;;
    *)       WHO="Your name is ${me}. You are a Fleetkick agent working solo on this tab. If you want help, add a teammate with the spawn tool." ;;
  esac
  RULES="$WHO Every agent in this group is looking at the SAME browser tab (tabId=$tab, browser install $install), so tab data is shared context, not something to re-establish. Control the browser ONLY with the fleetkick MCP tools (read, click, type, navigate, screenshot, tab_create, tabs_list, group, send_to_agent) — never claude-in-chrome. Your tools are bound to this ONE browser profile; several browsers can share this daemon and you only see your own."

  MCP_CFG="/tmp/fleetkick-mcp-$install-$tab-$slot.json"
  printf '{"mcpServers":{"fleetkick":{"command":"node","args":["%s/mcp.js"],"env":{"FLEETKICK_INSTALL":"%s","FLEETKICK_SESSION_TAB":"%s","FLEETKICK_SLOT":"%s","FLEETKICK_NAME":"%s"}}}}' \
    "$SCRIPT_DIR" "$install" "$tab" "$slot" "$me" > "$MCP_CFG"

  case "$agent" in
    claude) exec claude --mcp-config "$MCP_CFG" --append-system-prompt "$RULES" ;;
    grok)
      # grok has no per-invocation MCP config; it reads ~/.grok/config.toml. `mcp add` is an
      # upsert, so running it each time is idempotent. The server inherits this shell's env,
      # which is how FLEETKICK_INSTALL and the slot reach it.
      grok mcp add fleetkick --scope user node "$SCRIPT_DIR/mcp.js" >/dev/null 2>&1 || true
      exec grok --rules "$RULES"
      ;;
    # ponytail: codex/gemini get the env but no prompt injection — guessing at their prompt
    # flags would be untested code, and neither is in the demo path yet.
    *) exec "$agent" ;;
  esac
fi

if [ -z "${1:-}" ]; then
  exec claude
fi

# Outer wrapper: ttyd runs this per connection. $1=install $2=tabId $3=slot (default 0).
install="$1"; tab="${2:-}"; slot="${3:-0}"
ok_install "$install" || exit 1
ok_num "$tab" || exit 1
ok_num "$slot" || { slot=0; }
sess="fleetkick-$install-$tab"
[ "$slot" = "0" ] || sess="$sess-$slot"

# Re-attach forever instead of exec'ing once. A tmux client exits cleanly whenever it
# detaches — another client taking the session, a kill-session, a switch-client — and that
# clean exit ends the ttyd process, which is what parks the terminal on "Press Enter to
# Reconnect". ttyd's reconnect only covers a DROPPED socket, not a process that finished, so
# no client setting can fix it; the server has to refuse to end. tmux holds the session, so
# re-attaching is seamless rather than a new conversation.
fails=0
while :; do
  if tmux new-session -A -s "$sess" "$SCRIPT_DIR/boot.sh" --inner "$install" "$tab" "$slot"; then
    fails=0
  else
    fails=$((fails + 1))
  fi
  if [ "$fails" -ge 5 ]; then
    echo "fleetkick: tmux attach failed 5 times in a row — giving up rather than spinning" >&2
    exec sleep 86400
  fi
  sleep 0.3
done
