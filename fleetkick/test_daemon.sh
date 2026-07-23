#!/usr/bin/env bash
# Proves the daemon can't be shut down: kill -9 both ports, expect both back.
# Requires install-daemon.sh to have been run. Safe to run any time (~15s).
set -euo pipefail
LABEL="com.eidos.fleetkick"

launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1 || {
  echo "SKIP: $LABEL not loaded — run ./install-daemon.sh first"; exit 0; }

up() { curl -s -m 3 -H 'x-fleetkick: 1' http://127.0.0.1:7682/health | grep -q ok; }
ttyd_up() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:7681/)" = "200" ]; }

up && ttyd_up || { echo "FAIL: not healthy before the test"; exit 1; }
before=$(launchctl print "gui/$UID/$LABEL" | awk '/^\tpid =/{print $3}')

lsof -ti tcp:7682 | xargs kill -9 2>/dev/null || true
pkill -9 -f "ttyd -p 7681" 2>/dev/null || true

for i in $(seq 1 20); do sleep 1; if up && ttyd_up; then break; fi; done

up      || { echo "FAIL: bridge never came back"; exit 1; }
ttyd_up || { echo "FAIL: ttyd never came back"; exit 1; }
after=$(launchctl print "gui/$UID/$LABEL" | awk '/^\tpid =/{print $3}')
[ "$before" != "$after" ] || { echo "FAIL: pid unchanged ($before) — nothing actually restarted"; exit 1; }

echo "PASS: killed -9, launchd rebuilt it ($before -> $after)"
