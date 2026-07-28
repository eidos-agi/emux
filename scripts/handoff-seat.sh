#!/bin/sh
# Thin emux handoff (docs/handoff-procedure.md).
#
#   install  — KNOWLEDGE.md + this-chat-handoff.md + tmux seat + register
#   verify   — structural: seat up, knowledge present (>=5 lines)
#   boot     — optional: start claude if pane has no claude child
#   quiz     — optional: one-shot emux ask; sole-line READY_FOR_HANDOFF=yes
#   status   — print state
#
# Does NOT overwrite product CLAUDE.md / AGENTS.md / ALWAYS.md.
set -eu

HOME_DIR="${HOME:?}"
export PATH="$HOME_DIR/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

die() { echo "handoff: $*" >&2; exit 2; }
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
[ -n "$SEAT" ] || SEAT="${PRODUCT}-this-chat"

STATE_ROOT="${EMUX_HANDOFF_STATE_ROOT:-$HOME_DIR/.local/share}"
STATE="$STATE_ROOT/$SEAT"
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
    [ -d "$cand" ] && { printf '%s' "$cand"; return; }
  done
  die "repo not found for $PRODUCT (pass --repo)"
}

do_install() {
  REPO="$(resolve_repo)"
  [ -n "$KNOWLEDGE" ] || KNOWLEDGE="$REPO/KNOWLEDGE.md"
  [ -f "$KNOWLEDGE" ] || die "write KNOWLEDGE.md first: $KNOWLEDGE"

  # Authoritative pack only — never clobber product CLAUDE/ALWAYS/AGENTS.
  cp "$KNOWLEDGE" "$STATE/KNOWLEDGE.md"
  if [ "$(cd "$(dirname "$KNOWLEDGE")" && pwd)/$(basename "$KNOWLEDGE")" != \
       "$(cd "$REPO" && pwd)/KNOWLEDGE.md" ]; then
    cp "$KNOWLEDGE" "$REPO/KNOWLEDGE.md"
  fi

  {
    echo "# Handoff: $SEAT"
    echo
    echo "- Product: \`$PRODUCT\`"
    echo "- Seat: \`$SEAT\` — \`tmux attach -t $SEAT\`"
    echo "- Repo: \`$REPO\`"
    echo "- Knowledge: \`KNOWLEDGE.md\` (read this first)"
    [ -n "$SOURCE_SESSION" ] && echo "- Source: \`$SOURCE_SESSION\`"
    echo "- Structural check: \`emux handoff verify --product $PRODUCT\`"
  } > "$REPO/this-chat-handoff.md"
  cp "$REPO/this-chat-handoff.md" "$STATE/this-chat-handoff.md"

  if ! tmux has-session -t "$SEAT" 2>/dev/null; then
    tmux new-session -d -s "$SEAT" -c "$REPO"
    tmux send-keys -t "$SEAT" "clear; echo handoff seat $SEAT; ls KNOWLEDGE.md; pwd" Enter
  fi

  if command -v emux >/dev/null 2>&1; then
    emux register "$SEAT" "$SEAT" \
      --description "Handoff home for $PRODUCT — read KNOWLEDGE.md" \
      --tags "$PRODUCT,handoff,this-chat" 2>/dev/null || true
  fi

  echo "handoff: installed $SEAT"
  echo "  knowledge → $REPO/KNOWLEDGE.md"
  echo "  next: emux handoff verify --product $PRODUCT"
}

do_boot() {
  REPO="$(resolve_repo)"
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing — run install"
  ppid="$(tmux list-panes -t "$SEAT" -F '#{pane_pid}' 2>/dev/null | head -1)"
  if [ -n "$ppid" ] && pgrep -P "$ppid" -x claude >/dev/null 2>&1; then
    echo "handoff: claude already running"
    return 0
  fi
  cmd="$(tmux list-panes -t "$SEAT" -F '#{pane_current_command}' 2>/dev/null | head -1)"
  [ "$cmd" = "claude" ] && { echo "handoff: claude already running"; return 0; }

  tmux send-keys -t "$SEAT" C-c
  sleep 0.2
  tmux send-keys -t "$SEAT" "cd $(printf %q "$REPO") && claude --dangerously-skip-permissions" Enter
  echo "handoff: launched claude in $SEAT"
}

# Deterministic ship gate — no LLM.
do_verify() {
  REPO="$(resolve_repo)"
  fail=0
  tmux has-session -t "$SEAT" 2>/dev/null || {
    echo "handoff: FAIL tmux seat missing: $SEAT" >&2
    fail=1
  }
  [ -f "$STATE/KNOWLEDGE.md" ] || {
    echo "handoff: FAIL missing $STATE/KNOWLEDGE.md" >&2
    fail=1
  }
  [ -f "$REPO/KNOWLEDGE.md" ] || {
    echo "handoff: FAIL missing $REPO/KNOWLEDGE.md" >&2
    fail=1
  }
  if [ -f "$STATE/KNOWLEDGE.md" ]; then
    lines=$(wc -l < "$STATE/KNOWLEDGE.md" | tr -d ' ')
    [ "${lines:-0}" -ge 5 ] || {
      echo "handoff: FAIL KNOWLEDGE too short ($lines lines)" >&2
      fail=1
    }
  fi
  if [ "$fail" -ne 0 ]; then
    echo "fail" > "$STATE/verify-status"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-at"
    exit 3
  fi
  echo "handoff: VERIFY PASS (structural)"
  echo "pass" > "$STATE/verify-status"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-at"
  exit 0
}

# Optional one-shot. No retries. Source agent still grades substance.
do_quiz() {
  tmux has-session -t "$SEAT" 2>/dev/null || die "seat missing"
  REPO="$(resolve_repo)"
  [ -f "$REPO/KNOWLEDGE.md" ] || die "KNOWLEDGE.md missing"
  command -v emux >/dev/null 2>&1 || die "emux required"

  quiz="Read KNOWLEDGE.md. Short recap of compass + open gaps. Final line alone: READY_FOR_HANDOFF=yes if you can run without the source chat, else READY_FOR_HANDOFF=no."
  echo "handoff: quiz (one shot)…"
  out="$(emux ask "$SEAT" "$quiz" 2>&1)" || true
  printf '%s\n' "$out" | tail -30
  printf '%s\n' "$out" > "$STATE/last-quiz.txt"

  if printf '%s\n' "$out" | grep -E '^[[:space:]]*READY_FOR_HANDOFF=no[[:space:]]*$' >/dev/null; then
    echo "handoff: QUIZ FAIL (said no)" >&2
    echo "quiz-fail" > "$STATE/quiz-status"
    exit 3
  fi
  if printf '%s\n' "$out" | grep -E '^[[:space:]]*READY_FOR_HANDOFF=yes[[:space:]]*$' >/dev/null; then
    echo "handoff: QUIZ PASS"
    echo "quiz-pass" > "$STATE/quiz-status"
    exit 0
  fi
  echo "handoff: QUIZ FAIL (no sole-line READY token)" >&2
  echo "quiz-fail" > "$STATE/quiz-status"
  exit 3
}

do_status() {
  echo "product: $PRODUCT"
  echo "seat:    $SEAT"
  if tmux has-session -t "$SEAT" 2>/dev/null; then
    echo "tmux:    UP"
  else
    echo "tmux:    DOWN"
  fi
  if [ -f "$STATE/KNOWLEDGE.md" ]; then
    echo "knowledge: $STATE/KNOWLEDGE.md ($(wc -l < "$STATE/KNOWLEDGE.md" | tr -d ' ') lines)"
  else
    echo "knowledge: MISSING"
  fi
  [ -f "$STATE/verify-status" ] && echo "verify:  $(cat "$STATE/verify-status")" || echo "verify:  never"
  [ -f "$STATE/quiz-status" ] && echo "quiz:    $(cat "$STATE/quiz-status")" || echo "quiz:    never"
  command -v emux >/dev/null 2>&1 && emux ls 2>/dev/null | grep -F "$SEAT" || true
}

case "$cmd" in
  install) do_install ;;
  boot) do_boot ;;
  verify) do_verify ;;
  quiz) do_quiz ;;
  status) do_status ;;
  *) die "unknown command $cmd" ;;
esac
