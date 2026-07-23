#!/usr/bin/env bash
# Version discipline gate. Fails when:
#   1. manifest.json's version has no CHANGELOG entry (i.e. it was bumped without saying
#      what changed, or changed without being bumped), or
#   2. the extension Chrome has loaded is behind what's on disk — the stale-code trap that
#      twice looked like a code bug during development.
set -euo pipefail
cd "$(dirname "$0")"

disk=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' extension/manifest.json | head -1)
[ -n "$disk" ] || { echo "FAIL: no version in manifest.json"; exit 1; }

grep -q "^## $disk\$" CHANGELOG.md \
  || { echo "FAIL: manifest is $disk but CHANGELOG.md has no '## $disk' entry"; exit 1; }

top=$(sed -n 's/^## \(.*\)$/\1/p' CHANGELOG.md | head -1)
[ "$top" = "$disk" ] \
  || { echo "FAIL: CHANGELOG's newest entry is $top, manifest is $disk"; exit 1; }

# Runtime half — only when the daemon is up and Chrome is reachable.
if curl -s -m 3 -H 'x-fleetkick: 1' http://127.0.0.1:7682/health | grep -q ok; then
  # || reply='' — curl exits 28 on timeout, and under `set -e` that killed the whole script
  # with no output at all. A gate that dies silently is worse than no gate.
  reply=$(curl -s -m 15 -X POST -H 'content-type: application/json' -H 'x-fleetkick: 1' \
    -d '{"op":"version"}' http://127.0.0.1:7682/cmd) || reply=''
  loaded=$(sed -n 's/.*"version":"\([^"]*\)".*/\1/p' <<< "$reply")
  # An extension that answers "unknown op" is loaded but predates the version tool —
  # that's stale by definition, not unreachable. Don't let it pass as "can't tell".
  grep -q 'unknown op' <<< "$reply" \
    && { echo "FAIL: loaded extension predates the version tool — reload it (disk is $disk)"; exit 1; }
  if [ -z "$loaded" ]; then
    echo "PASS: $disk documented (extension not reachable — can't check what's loaded)"
    exit 0
  fi
  [ "$loaded" = "$disk" ] \
    || { echo "FAIL: Chrome has $loaded loaded, disk is $disk — reload the extension"; exit 1; }
  echo "PASS: $disk documented and loaded"
else
  echo "PASS: $disk documented (daemon down — can't check what's loaded)"
fi
