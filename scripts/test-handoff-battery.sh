#!/bin/sh
# Lean handoff battery — structural + units. No LLM thrash.
set -eu
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
unset EMUX_HANDOFF_STATE EMUX_HANDOFF_STATE_ROOT
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PASS=0
FAIL=0
ok() { echo "  OK  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

echo "=== handoff battery (lean) $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if uv run pytest tests/test_handoff_seat.py tests/test_handoff_ready_parse.py -q --tb=line; then
  ok "pytest"
else
  bad "pytest"
fi

sh -n scripts/handoff-seat.sh && ok "sh -n" || bad "sh -n"

ec=0
emux handoff verify --product x --seat no-seat-xyz 2>/dev/null || ec=$?
[ "$ec" -eq 2 ] && ok "missing seat exit 2" || bad "missing seat ec=$ec"

TMP=$(mktemp -d)
ec=0
emux handoff install --product bare --repo "$TMP" --knowledge "$TMP/no.md" --seat bare-$$ 2>/dev/null || ec=$?
[ "$ec" -eq 2 ] && ok "missing knowledge exit 2" || bad "missing knowledge ec=$ec"
rm -rf "$TMP"

# Isolation
TMP=$(mktemp -d)
export EMUX_HANDOFF_STATE_ROOT="$TMP/state"
for i in 1 2; do
  mkdir -p "$TMP/p$i"
  echo "TOKEN_$i" >"$TMP/p$i/KNOWLEDGE.md"
  # pad to >=5 lines for structural verify
  printf 'line2\nline3\nline4\nline5\n' >>"$TMP/p$i/KNOWLEDGE.md"
  emux handoff install --product "iso$i" --repo "$TMP/p$i" --knowledge "$TMP/p$i/KNOWLEDGE.md" \
    --seat "iso-$i-$$" --source-session s$i >/dev/null
done
grep -q TOKEN_1 "$TMP/state/iso-1-$$/KNOWLEDGE.md" && ok "iso1" || bad "iso1"
grep -q TOKEN_2 "$TMP/state/iso-2-$$/KNOWLEDGE.md" && ok "iso2" || bad "iso2"
grep -q TOKEN_2 "$TMP/state/iso-1-$$/KNOWLEDGE.md" && bad "leak" || ok "no leak"
# structural verify on fixture
emux handoff verify --product iso1 --repo "$TMP/p1" --seat "iso-1-$$" >/dev/null && ok "struct verify fixture" || bad "struct verify fixture"
tmux kill-session -t "iso-1-$$" 2>/dev/null || true
tmux kill-session -t "iso-2-$$" 2>/dev/null || true
emux unregister "iso-1-$$" 2>/dev/null || true
emux unregister "iso-2-$$" 2>/dev/null || true
unset EMUX_HANDOFF_STATE_ROOT
rm -rf "$TMP"

if [ -f "${HOME}/repos-aic/directmux/KNOWLEDGE.md" ]; then
  emux handoff install --product directmux --repo "${HOME}/repos-aic/directmux" \
    --knowledge "${HOME}/repos-aic/directmux/KNOWLEDGE.md" \
    --source-session battery >/dev/null && ok "directmux install" || bad "directmux install"
  emux handoff verify --product directmux --repo "${HOME}/repos-aic/directmux" >/dev/null \
    && ok "directmux structural verify" || bad "directmux structural verify"
  "${HOME}/repos-aic/directmux/bin/handoff" verify >/dev/null && ok "wrapper structural verify" || bad "wrapper verify"
  emux handoff status --product directmux 2>/dev/null | grep -q "verify:  pass" && ok "status pass" || bad "status"
else
  bad "no directmux knowledge"
fi

echo ""
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "BATTERY: PASS" && exit 0
echo "BATTERY: FAIL"
exit 1
