#!/bin/sh
# Lean structural handoff battery (no LLM thrash).
set -eu
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:$PATH"
unset EMUX_HANDOFF_STATE EMUX_HANDOFF_STATE_ROOT
cd "$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0
ok() { echo "  OK  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

echo "=== handoff lean battery ==="
uv run pytest tests/test_handoff_seat.py tests/test_handoff_ready_parse.py -q --tb=line \
  && ok pytest || bad pytest
sh -n scripts/handoff-seat.sh && ok "sh -n" || bad "sh -n"

ec=0
emux handoff verify --product x --seat no-seat 2>/dev/null || ec=$?
[ "$ec" -eq 2 ] || [ "$ec" -eq 3 ] && ok "missing seat fails" || bad "missing seat ec=$ec"

TMP=$(mktemp -d)
export EMUX_HANDOFF_STATE_ROOT="$TMP/state"
mkdir -p "$TMP/p"
printf 'line1\nline2\nline3\nline4\nline5\nTOKEN_A\n' >"$TMP/p/KNOWLEDGE.md"
emux handoff install --product lean --repo "$TMP/p" --knowledge "$TMP/p/KNOWLEDGE.md" \
  --seat "lean-$$" --source-session t >/dev/null && ok install || bad install
emux handoff verify --product lean --repo "$TMP/p" --seat "lean-$$" >/dev/null \
  && ok "structural verify" || bad "structural verify"
# must not create CLAUDE.md from knowledge overwrite policy
[ ! -f "$TMP/p/CLAUDE.md" ] && ok "no CLAUDE clobber" || bad "CLAUDE was written"
tmux kill-session -t "lean-$$" 2>/dev/null || true
emux unregister "lean-$$" 2>/dev/null || true
unset EMUX_HANDOFF_STATE_ROOT
rm -rf "$TMP"

if [ -f "${HOME}/repos-aic/directmux/KNOWLEDGE.md" ]; then
  emux handoff verify --product directmux --repo "${HOME}/repos-aic/directmux" >/dev/null \
    && ok "directmux verify" || bad "directmux verify"
fi

echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && exit 0
exit 1
