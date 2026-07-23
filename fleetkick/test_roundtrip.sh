#!/usr/bin/env bash
# Proves the control path without Chrome: mcp.js -> bridge.js -> (fake extension) -> back.
# Fails loudly if the queue, dispatch, or tabId targeting breaks.
set -euo pipefail
cd "$(dirname "$0")"

pkill -f "fleetkick/bridge.js" 2>/dev/null || true
node bridge.js > /tmp/fk-test-bridge.log 2>&1 &
BRIDGE=$!
trap 'kill $BRIDGE 2>/dev/null || true' EXIT
sleep 1

# Fake extension: take one command off the queue, answer it.
(
  cmd=$(curl -s -m 25 http://127.0.0.1:7682/pull)
  echo "$cmd" > /tmp/fk-test-cmd.json
  id=$(echo "$cmd" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
  curl -s -X POST -H 'content-type: application/json' \
    -d "{\"id\":$id,\"result\":{\"title\":\"Fake Page\"}}" http://127.0.0.1:7682/result > /dev/null
) &
sleep 0.5

out=$(printf '%s\n' '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"read","arguments":{}}}' \
      | FLEETKICK_TAB=42 timeout 10 node mcp.js)

grep -q 'Fake Page' <<< "$out"                    || { echo "FAIL: result never came back: $out"; exit 1; }
grep -q '"tabId":42' /tmp/fk-test-cmd.json        || { echo "FAIL: paired tabId not sent: $(cat /tmp/fk-test-cmd.json)"; exit 1; }
grep -q '"op":"read"' /tmp/fk-test-cmd.json       || { echo "FAIL: wrong op: $(cat /tmp/fk-test-cmd.json)"; exit 1; }

# A web page must not be able to drive the bridge.
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Origin: https://evil.example' http://127.0.0.1:7682/health)
[ "$code" = "403" ]                               || { echo "FAIL: web origin got $code, expected 403"; exit 1; }

echo "PASS: control path + origin guard"
