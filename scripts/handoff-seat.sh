#!/bin/sh
# Permanent emux handoff seat installer (see docs/handoff-procedure.md).
#
# Contract (keep simple — do not overfit Claude UI flakiness):
#   install  — durable KNOWLEDGE + seat + registry (deterministic)
#   boot     — start Claude if the pane is still a plain shell
#   verify   — structural gate only (files + tmux + registry)
#   quiz     — optional one-shot LLM recap; sole-line READY token (no retries)
#   status   — print state
#
# Usage:
#   handoff-seat.sh install --product P --repo PATH --knowledge PATH [--source-session ID] [--seat NAME]
#   handoff-seat.sh boot    --product P [--repo PATH] [--seat NAME]
#   handoff-seat.sh verify  --product P [--repo PATH] [--seat NAME]
#   handoff-seat.sh quiz    --product P [--repo PATH] [--seat NAME] [--timeout SECS]
#   handoff-seat.sh status  --product P [--seat NAME]
set -eu

HOME_DIR="${HOME:?}"
export PATH="$HOME_DIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

die() { echo "handoff-seat: $*" >&2; exit 2; }
cmd="${1:-}"; [ -n "$cmd" ] || die "need install|boot|verify|quiz|status"
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
CASE_PRODUCT="$PRODUCT"
case "$PRODUCT" in
  directmux|direct-mux|director|meta) CASE_PRODUCT="directrux" ;;
esac

if [ -z "$SEAT" ]; then
  SEAT="${PRODUCT}-this-chat"
fi

STATE_ROOT="${EMUX_HANDOFF_STATE_ROOT:-${EMUX_HANDOFF_STATE:-$HOME_DIR/.local/share}}"
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

  cp "$KNOWLEDGE" "$STATE/KNOWLEDGE.md"
  if [ "$(cd "$(dirname "$KNOWLEDGE")" && pwd)/$(basename "$KNOWLEDGE")" != \
       "$(cd "$REPO" && pwd)/KNOWLEDGE.md" ]; then
    cp "$KNOWLEDGE" "$REPO/KNOWLEDGE.md"
  fi

  for f in CLAUDE.md AGENTS.md STANDING.md; do
    cp "$KNOWLEDGE" "$STATE/$f"
    cp "$KNOWLEDGE" "$REPO/$f"
  done

  {
    echo "# Handoff: $SEAT"
    echo
    echo "- **Product:** $PRODUCT (case paths: $CASE_PRODUCT)"
    echo "- **Seat:** \`$SEAT\`"
    echo "- **Repo:** \`$REPO\`"
    echo "- **Knowledge:** \`KNOWLEDGE.md\` (authoritative)"
    [ -n "$SOURCE_SESSION" ] && echo "- **Source session:** \`$SOURCE_SESSION\`"
    echo "- **Open:** \`tmux attach -t $SEAT\`"
    echo "- **Structural verify:** \`emux handoff verify --product $PRODUCT\`"
    echo "- **Optional quiz:** \`emux handoff quiz --product $PRODUCT\`"
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
  echo "  next:      emux handoff verify --product $PRODUCT   # structural"
  echo "           emux handoff boot --product $PRODUCT    # optional"
  echo "           emux handoff quiz --product $PRODUCT    # optional LLM grade"
}

do_boot() {
  REPO="$(resolve_repo)"
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing — run install first"
  # Simple: if a claude process is already under this pane's process tree, skip.
  ppid="$(tmux list-panes -t "$SEAT" -F '#{pane_pid}' 2>/dev/null | head -1)"
  if [ -n "$ppid" ] && pgrep -P "$ppid" -x claude >/dev/null 2>&1; then
    echo "handoff-seat: claude already running under pane pid $ppid"
    return 0
  fi
  # Also skip if pane command is literally claude
  cmd="$(tmux list-panes -t "$SEAT" -F '#{pane_current_command}' 2>/dev/null | head -1)"
  case "$cmd" in
    claude)
      echo "handoff-seat: pane command is claude"
      return 0
      ;;
  esac
  tmux send-keys -t "$SEAT" C-c
  sleep 0.2
  tmux send-keys -t "$SEAT" "cd $(printf %q "$REPO") && claude --dangerously-skip-permissions" Enter
  echo "handoff-seat: launched claude in $SEAT"
}

# Structural gate only — deterministic, no LLM (default "verify").
do_verify() {
  REPO="$(resolve_repo)"
  fail=0
  if ! tmux has-session -t "$SEAT" 2>/dev/null; then
    echo "handoff-seat: VERIFY FAIL — tmux seat missing: $SEAT" >&2
    fail=1
  fi
  if [ ! -f "$STATE/KNOWLEDGE.md" ]; then
    echo "handoff-seat: VERIFY FAIL — state knowledge missing: $STATE/KNOWLEDGE.md" >&2
    fail=1
  fi
  if [ ! -f "$REPO/KNOWLEDGE.md" ]; then
    echo "handoff-seat: VERIFY FAIL — repo knowledge missing: $REPO/KNOWLEDGE.md" >&2
    fail=1
  fi
  if [ ! -f "$REPO/this-chat-handoff.md" ]; then
    echo "handoff-seat: VERIFY FAIL — this-chat-handoff.md missing" >&2
    fail=1
  fi
  # Knowledge must be non-trivial
  if [ -f "$STATE/KNOWLEDGE.md" ]; then
    lines=$(wc -l < "$STATE/KNOWLEDGE.md" | tr -d ' ')
    if [ "${lines:-0}" -lt 5 ]; then
      echo "handoff-seat: VERIFY FAIL — KNOWLEDGE.md too short ($lines lines)" >&2
      fail=1
    fi
  fi

  if [ "$fail" -ne 0 ]; then
    echo "fail" > "$STATE/verify-status"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-at"
    exit 3
  fi

  echo "handoff-seat: VERIFY PASS (structural)"
  echo "  seat:      $SEAT (tmux up)"
  echo "  knowledge: $STATE/KNOWLEDGE.md"
  echo "  handoff:   $REPO/this-chat-handoff.md"
  echo "pass" > "$STATE/verify-status"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-at"
  exit 0
}

# Optional one-shot LLM quiz. No retries. Sole-line READY only.
# Source agent should still grade substance; this only checks the token.
do_quiz() {
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing"
  REPO="$(resolve_repo)"
  [ -f "$REPO/KNOWLEDGE.md" ] || die "KNOWLEDGE.md missing in $REPO"
  command -v emux >/dev/null 2>&1 || die "emux CLI required for quiz"

  quiz="Read KNOWLEDGE.md in the repo root. Briefly recap compass, product, proved green, and not-fixed. After the recap put the token READY_FOR_HANDOFF=yes alone on its own final line if you can operate without the source chat; otherwise READY_FOR_HANDOFF=no alone on its own final line."

  echo "handoff-seat: quiz (one shot, timeout ${TIMEOUT}s)…"
  out="$(emux ask "$SEAT" "$quiz" 2>&1)" || true
  printf '%s\n' "$out" | tail -40
  printf '%s\n' "$out" > "$STATE/last-quiz.txt"

  # Sole-line only — never match the instruction sentence mid-line.
  if printf '%s\n' "$out" | grep -E '^[[:space:]]*READY_FOR_HANDOFF=no[[:space:]]*$' >/dev/null; then
    echo "handoff-seat: QUIZ FAIL (READY_FOR_HANDOFF=no)" >&2
    echo "quiz-fail" > "$STATE/quiz-status"
    exit 3
  fi
  if printf '%s\n' "$out" | grep -E '^[[:space:]]*READY_FOR_HANDOFF=yes[[:space:]]*$' >/dev/null; then
    echo "handoff-seat: QUIZ PASS"
    echo "quiz-pass" > "$STATE/quiz-status"
    exit 0
  fi
  echo "handoff-seat: QUIZ FAIL (no sole-line READY token in ask output)" >&2
  echo "quiz-fail" > "$STATE/quiz-status"
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
  if [ -f "$STATE/quiz-status" ]; then
    echo "quiz:    $(cat "$STATE/quiz-status")"
  else
    echo "quiz:    never"
  fi
  if command -v emux >/dev/null 2>&1; then
    emux ls 2>/dev/null | grep -F "$SEAT" || echo "registry: (not listed)"
  fi
}

case "$cmd" in
  install) do_install ;;
  boot) do_boot ;;
  verify) do_verify ;;
  quiz) do_quiz ;;
  status) do_status ;;
  *) die "unknown command $cmd (want install|boot|verify|quiz|status)" ;;
esac
