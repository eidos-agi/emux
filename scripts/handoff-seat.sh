#!/bin/sh
# Permanent emux handoff seat installer (see docs/handoff-procedure.md).
# Usage:
#   handoff-seat.sh install  --product P --repo PATH --knowledge PATH [--source-session ID] [--seat NAME]
#   handoff-seat.sh boot     --product P [--seat NAME]
#   handoff-seat.sh verify   --product P [--seat NAME] [--timeout SECS]
#   handoff-seat.sh status   --product P [--seat NAME]
set -eu

HOME_DIR="${HOME:?}"
export PATH="$HOME_DIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

die() { echo "handoff-seat: $*" >&2; exit 2; }
cmd="${1:-}"; [ -n "$cmd" ] || die "need install|boot|verify|status"
shift

PRODUCT=""
REPO=""
KNOWLEDGE=""
SOURCE_SESSION=""
SEAT=""
TIMEOUT=120

while [ $# -gt 0 ]; do
  case "$1" in
    --product) PRODUCT="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --knowledge) KNOWLEDGE="${2:-}"; shift 2 ;;
    --source-session) SOURCE_SESSION="${2:-}"; shift 2 ;;
    --seat) SEAT="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[ -n "$PRODUCT" ] || die "--product required"
PRODUCT="$(printf '%s' "$PRODUCT" | tr '[:upper:]' '[:lower:]')"
# Brand alias: directmux uses directrux paths for compat
CASE_PRODUCT="$PRODUCT"
case "$PRODUCT" in
  directmux|direct-mux|director|meta) CASE_PRODUCT="directrux" ;;
esac

if [ -z "$SEAT" ]; then
  SEAT="${PRODUCT}-this-chat"
fi

# EMUX_HANDOFF_STATE_ROOT = parent dir for seat state (default ~/.local/share).
# Each seat always gets its own $ROOT/$SEAT subdirectory.
STATE_ROOT="${EMUX_HANDOFF_STATE_ROOT:-${EMUX_HANDOFF_STATE:-$HOME_DIR/.local/share}}"
# If caller passed a full seat path as EMUX_HANDOFF_STATE (legacy tests), use it only when
# it already ends with the seat name; otherwise treat as root.
case "$STATE_ROOT" in
  */"$SEAT") STATE="$STATE_ROOT" ;;
  *) STATE="$STATE_ROOT/$SEAT" ;;
esac
mkdir -p "$STATE"

resolve_repo() {
  if [ -n "$REPO" ] && [ -d "$REPO" ]; then
    printf '%s' "$REPO"
    return
  fi
  for cand in \
    "$HOME_DIR/repos-aic/$PRODUCT" \
    "$HOME_DIR/repos-aic/$CASE_PRODUCT" \
    "$HOME_DIR/repos-eidos-agi/$PRODUCT" \
    "$HOME_DIR/repos-eidos-agi/$CASE_PRODUCT"
  do
    if [ -d "$cand" ]; then
      printf '%s' "$cand"
      return
    fi
  done
  die "could not find product repo for $PRODUCT (pass --repo)"
}

do_install() {
  REPO="$(resolve_repo)"
  [ -n "$KNOWLEDGE" ] || KNOWLEDGE="$REPO/KNOWLEDGE.md"
  [ -f "$KNOWLEDGE" ] || die "knowledge file missing: $KNOWLEDGE (write KNOWLEDGE.md first)"

  # Copy knowledge into state always; into repo only if different path
  cp "$KNOWLEDGE" "$STATE/KNOWLEDGE.md"
  if [ "$(cd "$(dirname "$KNOWLEDGE")" && pwd)/$(basename "$KNOWLEDGE")" != \
       "$(cd "$REPO" && pwd)/KNOWLEDGE.md" ]; then
    cp "$KNOWLEDGE" "$REPO/KNOWLEDGE.md"
  fi

  # Product-owned briefs in seat + repo (home agent always sees them)
  for f in CLAUDE.md AGENTS.md STANDING.md; do
    cp "$KNOWLEDGE" "$STATE/$f"
    cp "$KNOWLEDGE" "$REPO/$f"
  done

  # Human pointer
  {
    echo "# Handoff: $SEAT"
    echo
    echo "- **Product:** $PRODUCT (case paths: $CASE_PRODUCT)"
    echo "- **Seat:** \`$SEAT\`"
    echo "- **Repo:** \`$REPO\`"
    echo "- **Knowledge:** \`KNOWLEDGE.md\` (authoritative)"
    [ -n "$SOURCE_SESSION" ] && echo "- **Source session:** \`$SOURCE_SESSION\`"
    echo "- **Open:** \`tmux attach -t $SEAT\`"
    echo "- **Verify:** \`emux handoff verify --product $PRODUCT\`"
    echo "- **Procedure:** emux \`docs/handoff-procedure.md\`"
  } > "$REPO/this-chat-handoff.md"
  cp "$REPO/this-chat-handoff.md" "$STATE/this-chat-handoff.md"

  if ! tmux has-session -t "$SEAT" 2>/dev/null; then
    tmux new-session -d -s "$SEAT" -c "$REPO"
    tmux send-keys -t "$SEAT" "clear; echo 'handoff seat: $SEAT'; ls KNOWLEDGE.md STANDING.md 2>/dev/null; pwd" Enter
  fi

  if command -v emux >/dev/null 2>&1; then
    emux register "$SEAT" "$SEAT" \
      --description "Handoff home for $PRODUCT — read KNOWLEDGE.md; source=$SOURCE_SESSION" \
      --tags "$PRODUCT,handoff,this-chat" \
      2>/dev/null || emux register "$SEAT" "$SEAT" 2>/dev/null || true
  fi

  echo "handoff-seat: installed"
  echo "  seat:      $SEAT"
  echo "  repo:      $REPO"
  echo "  knowledge: $REPO/KNOWLEDGE.md"
  echo "  state:     $STATE"
  echo "  next:      emux handoff boot --product $PRODUCT && emux handoff verify --product $PRODUCT"
}

do_boot() {
  REPO="$(resolve_repo)"
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing — run install first"
  # Already Claude?
  cmd="$(tmux list-panes -t "$SEAT" -F '#{pane_current_command}' 2>/dev/null | head -1)"
  case "$cmd" in
    claude|node|python*)
      echo "handoff-seat: pane already running $cmd"
      return 0
      ;;
  esac
  tmux send-keys -t "$SEAT" C-c
  sleep 0.2
  tmux send-keys -t "$SEAT" "cd $(printf %q "$REPO") && claude --dangerously-skip-permissions" Enter
  echo "handoff-seat: launched claude in $SEAT (wait a few seconds before verify)"
}

do_verify() {
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing"
  REPO="$(resolve_repo)"
  [ -f "$REPO/KNOWLEDGE.md" ] || die "KNOWLEDGE.md missing in $REPO"

  if ! command -v emux >/dev/null 2>&1; then
    die "emux CLI required for verify"
  fi

  quiz="Read KNOWLEDGE.md fully in the repo root. Answer structured: 1. Compass four bullets. 2. Product role and what it manages and room. 3. What was proved green with commands and numbers. 4. What is NOT fixed honest. 5. Dogfood commands. End with exactly READY_FOR_HANDOFF=yes or READY_FOR_HANDOFF=no. Use =yes only if you can operate without the source chat thread."

  echo "handoff-seat: verifying $SEAT (timeout ${TIMEOUT}s)…"
  out="$(emux ask "$SEAT" "$quiz" 2>&1)" || true
  printf '%s\n' "$out" | tail -60
  printf '%s\n' "$out" > "$STATE/last-verify.txt"

  if printf '%s\n' "$out" | grep -q 'READY_FOR_HANDOFF=yes'; then
    echo "handoff-seat: VERIFY PASS"
    echo "pass" > "$STATE/verify-status"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-at"
    exit 0
  fi
  echo "handoff-seat: VERIFY FAIL (no READY_FOR_HANDOFF=yes)" >&2
  echo "fail" > "$STATE/verify-status"
  exit 3
}

do_status() {
  echo "product: $PRODUCT"
  echo "seat:    $SEAT"
  if tmux has-session -t "$SEAT" 2>/dev/null; then
    echo "tmux:    UP ($(tmux list-panes -t "$SEAT" -F '#{pane_current_command}'))"
  else
    echo "tmux:    DOWN"
  fi
  if [ -f "$STATE/KNOWLEDGE.md" ]; then
    lines=$(wc -l < "$STATE/KNOWLEDGE.md" | tr -d ' ')
    echo "knowledge: $STATE/KNOWLEDGE.md ($lines lines)"
  else
    echo "knowledge: MISSING"
  fi
  if [ -f "$STATE/verify-status" ]; then
    echo "verify:  $(cat "$STATE/verify-status") @ $(cat "$STATE/verified-at" 2>/dev/null || echo '?')"
  else
    echo "verify:  never"
  fi
  if command -v emux >/dev/null 2>&1; then
    emux ls 2>/dev/null | grep -F "$SEAT" || echo "registry: (not listed)"
  fi
}

case "$cmd" in
  install) do_install ;;
  boot) do_boot ;;
  verify) do_verify ;;
  status) do_status ;;
  *) die "unknown command $cmd" ;;
esac
