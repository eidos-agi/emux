#!/usr/bin/env bash
# Proves the control path without Chrome: mcp.js -> bridge.js -> (fake extension) -> back.
# Fails loudly if the queue, dispatch, install routing, or tabId targeting breaks.
set -euo pipefail
cd "$(dirname "$0")"

# Own port, so a live extension polling the real bridge can't steal our commands
# (and so running the test doesn't knock the real one over).
export PORT=7699
# A fake browser profile. Real ones are minted by the extension; the shape is 8 hex chars.
IID=deadbeef
FLEETKICK_PORT=$PORT node bridge.js > /tmp/fk-test-bridge.log 2>&1 &
BRIDGE=$!
trap 'kill $BRIDGE 2>/dev/null || true' EXIT
sleep 1

# Fake extension: take one command off OUR install's queue, answer it.
poll_once() {
  cmd=$(curl -s -m 25 -H 'x-fleetkick: 1' "http://127.0.0.1:$PORT/pull?install=$IID&exec=aaaaaaaa") || return 0
  echo "$cmd" > "$1"
  id=$(echo "$cmd" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
  curl -s -X POST -H 'content-type: application/json' -H 'x-fleetkick: 1' \
    -d "{\"id\":$id,\"result\":{\"title\":\"Fake Page\"}}" "http://127.0.0.1:$PORT/result" > /dev/null
}

poll_once /tmp/fk-test-cmd.json &
sleep 0.5

out=$(printf '%s\n' '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"read","arguments":{}}}' \
      | FLEETKICK_TAB=42 FLEETKICK_INSTALL=$IID FLEETKICK_PORT=$PORT timeout 10 node mcp.js)

grep -q 'Fake Page' <<< "$out"                    || { echo "FAIL: result never came back: $out"; exit 1; }
grep -q '"tabId":42' /tmp/fk-test-cmd.json        || { echo "FAIL: pinned tabId not sent: $(cat /tmp/fk-test-cmd.json)"; exit 1; }
grep -q '"op":"read"' /tmp/fk-test-cmd.json       || { echo "FAIL: wrong op: $(cat /tmp/fk-test-cmd.json)"; exit 1; }
# install is a routing detail. If it leaks into the command the extension would try to
# execute it as a field, and a future op named "install" would collide with it.
grep -q '"install"' /tmp/fk-test-cmd.json         && { echo "FAIL: install leaked into the command: $(cat /tmp/fk-test-cmd.json)"; exit 1; }

# Unpinned (the normal case): no tabId rides along, so the extension resolves the
# selected tab itself. A stale tabId sneaking in here is the bug this guards.
poll_once /tmp/fk-test-cmd2.json &
sleep 0.5
printf '%s\n' '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"read","arguments":{}}}' \
  | FLEETKICK_INSTALL=$IID FLEETKICK_PORT=$PORT timeout 10 node mcp.js > /dev/null
grep -q '"tabId"' /tmp/fk-test-cmd2.json          && { echo "FAIL: unpinned call still sent a tabId: $(cat /tmp/fk-test-cmd2.json)"; exit 1; }

# An unidentified poller would land back in the shared-queue world this replaced, where a
# command goes to whichever browser grabbed it first.
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'x-fleetkick: 1' "http://127.0.0.1:$PORT/pull")
[ "$code" = "400" ]                               || { echo "FAIL: /pull without an install id got $code, expected 400"; exit 1; }

# A tabId reaches tmux via execFile argv, but it also builds a session name — so a
# non-numeric one must never get that far. Same for the install id.
inj=$(curl -s -X POST -H 'content-type: application/json' -H 'x-fleetkick: 1' \
  -d "{\"install\":\"$IID\",\"tabId\":\"1; touch /tmp/fk-pwned\"}" "http://127.0.0.1:$PORT/switch")
grep -q 'bad tabId' <<< "$inj"                    || { echo "FAIL: injection not rejected: $inj"; exit 1; }
[ ! -e /tmp/fk-pwned ]                            || { rm -f /tmp/fk-pwned; echo "FAIL: injection executed"; exit 1; }

inj2=$(curl -s -X POST -H 'content-type: application/json' -H 'x-fleetkick: 1' \
  -d '{"install":"../../etc","tabId":"1"}' "http://127.0.0.1:$PORT/switch")
grep -q 'bad install id' <<< "$inj2"              || { echo "FAIL: bad install id not rejected: $inj2"; exit 1; }

# The dropdown needs a list; an array (even empty) is the contract.
grep -q '^\[' <<< "$(curl -s -H 'x-fleetkick: 1' "http://127.0.0.1:$PORT/sessions?install=$IID")" \
                                                  || { echo "FAIL: /sessions did not return an array"; exit 1; }

# A web page must not be able to drive the bridge.
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Origin: https://evil.example' -H 'x-fleetkick: 1' "http://127.0.0.1:$PORT/health")
[ "$code" = "403" ]                               || { echo "FAIL: web origin got $code, expected 403"; exit 1; }

echo "PASS: control path + install routing + origin guard"
