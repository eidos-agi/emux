#!/bin/sh
# Extended handoff battery. Exit 0 only if all checks pass.
set -eu
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
unset EMUX_HANDOFF_STATE EMUX_HANDOFF_STATE_ROOT
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PASS=0
FAIL=0
ok() { echo "  OK  $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

echo "=== handoff battery $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

echo "-- unit --"
if uv run pytest tests/test_handoff_seat.py tests/test_handoff_ready_parse.py -q --tb=line; then
  ok "pytest handoff suite"
else
  bad "pytest handoff suite"
fi

echo "-- negatives --"
ec=0
emux handoff verify --product x --seat no-seat-xyz 2>/dev/null || ec=$?
[ "$ec" -eq 2 ] && ok "missing seat exit 2" || bad "missing seat exit=$ec"

TMP=$(mktemp -d)
ec=0
emux handoff install --product bare --repo "$TMP" --knowledge "$TMP/missing.md" --seat bare-$$ 2>/dev/null || ec=$?
[ "$ec" -eq 2 ] && ok "missing knowledge exit 2" || bad "missing knowledge exit=$ec"
rm -rf "$TMP"

echo "-- shell-only must fail verify --"
TMP=$(mktemp -d)
export EMUX_HANDOFF_STATE_ROOT="$TMP/state"
SEAT="shellbat-$$"
mkdir -p "$TMP/p"
printf '# k\nCompass\n' >"$TMP/p/KNOWLEDGE.md"
emux handoff install --product shellbat --repo "$TMP/p" --knowledge "$TMP/p/KNOWLEDGE.md" \
  --seat "$SEAT" --source-session t >/dev/null
tmux send-keys -t "$SEAT" "echo only-shell" Enter 2>/dev/null || true
ec=0
emux handoff verify --product shellbat --repo "$TMP/p" --seat "$SEAT" --timeout 20 >/dev/null 2>&1 || ec=$?
[ "$ec" -ne 0 ] && ok "shell-only verify fails (ec=$ec)" || bad "shell-only falsely passed"
tmux kill-session -t "$SEAT" 2>/dev/null || true
emux unregister "$SEAT" 2>/dev/null || true
unset EMUX_HANDOFF_STATE_ROOT
rm -rf "$TMP"

echo "-- isolation --"
TMP=$(mktemp -d)
export EMUX_HANDOFF_STATE_ROOT="$TMP/state"
for i in 1 2; do
  mkdir -p "$TMP/p$i"
  echo "TOKEN_$i" >"$TMP/p$i/KNOWLEDGE.md"
  emux handoff install --product "iso$i" --repo "$TMP/p$i" --knowledge "$TMP/p$i/KNOWLEDGE.md" \
    --seat "iso-$i-$$" --source-session s$i >/dev/null
done
grep -q TOKEN_1 "$TMP/state/iso-1-$$/KNOWLEDGE.md" && ok "iso seat1" || bad "iso seat1"
grep -q TOKEN_2 "$TMP/state/iso-2-$$/KNOWLEDGE.md" && ok "iso seat2" || bad "iso seat2"
grep -q TOKEN_2 "$TMP/state/iso-1-$$/KNOWLEDGE.md" && bad "cross leak" || ok "no cross leak"
tmux kill-session -t "iso-1-$$" 2>/dev/null || true
tmux kill-session -t "iso-2-$$" 2>/dev/null || true
emux unregister "iso-1-$$" 2>/dev/null || true
emux unregister "iso-2-$$" 2>/dev/null || true
unset EMUX_HANDOFF_STATE_ROOT
rm -rf "$TMP"

echo "-- directmux live --"
if [ -f "${HOME}/repos-aic/directmux/KNOWLEDGE.md" ]; then
  emux handoff install --product directmux --repo "${HOME}/repos-aic/directmux" \
    --knowledge "${HOME}/repos-aic/directmux/KNOWLEDGE.md" \
    --source-session battery >/dev/null && ok "directmux reinstall" || bad "directmux reinstall"
  emux handoff boot --product directmux --repo "${HOME}/repos-aic/directmux" >/dev/null && ok "directmux boot" || bad "directmux boot"
  sleep 3
  if emux handoff verify --product directmux --repo "${HOME}/repos-aic/directmux" --timeout 180 >/tmp/handoff-bat-dm.out 2>&1; then
    grep -q "VERIFY PASS" /tmp/handoff-bat-dm.out && ok "directmux verify" || bad "directmux verify msg"
  else
    bad "directmux verify exit"
    tail -15 /tmp/handoff-bat-dm.out || true
  fi
  if "${HOME}/repos-aic/directmux/bin/handoff" status 2>/dev/null | grep -q directmux-this-chat; then
    ok "wrapper status"
  else
    bad "wrapper status"
  fi
  if "${HOME}/repos-aic/directmux/bin/handoff" verify >/tmp/handoff-bat-wrap.out 2>&1; then
    grep -q "VERIFY PASS" /tmp/handoff-bat-wrap.out && ok "wrapper verify" || bad "wrapper verify msg"
  else
    bad "wrapper verify exit"
  fi
  st=$(emux handoff status --product directmux 2>/dev/null || true)
  echo "$st" | grep -q "verify:  pass" && ok "status shows pass" || bad "status pass line"
  echo "$st" | grep -q ".local/share/directmux-this-chat" && ok "knowledge path home" || bad "knowledge path"
else
  bad "directmux KNOWLEDGE.md missing — skip live"
fi

echo "-- concurrent status x8 --"
i=0
while [ "$i" -lt 8 ]; do
  emux handoff status --product directmux >/dev/null 2>&1 || bad "concurrent status $i"
  i=$((i + 1))
done
ok "concurrent status x8"

echo ""
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "BATTERY: PASS"
  exit 0
fi
echo "BATTERY: FAIL"
exit 1
