# Changelog

Version lives in `extension/manifest.json` and is the single source of truth. The panel
footer and the `version` tool both report the **loaded** version, so a stale extension is
visible instead of being something you have to deduce.

## 0.7.0

- Per-install identity. Every extension install mints an 8-hex `installId` (owned by the
  worker, kept in `chrome.storage.local`), and every offscreen run mints an `executionId`.
  This fixes a live bug, not a hypothetical one: tab ids are unique only *within* a browser
  profile, so two Chromium browsers holding the same tab id mapped to the **same tmux
  session**, each believing it was alone. The bridge's single global queue had the matching
  flaw — it handed each command to whichever browser polled first, so you could type in one
  browser and watch it act in another.
- Sessions are now `fleetkick-<install>-<tabId>`; `/pull` requires an install id; `/cmd` and
  `/switch` route by one, and refuse with the list of connected browsers when it's ambiguous
  rather than guessing. `/sessions` filters by install, so a browser only sees its own.
- `switch-client` only moves clients belonging to the same install — otherwise one browser's
  tab change would yank the terminal out from under another browser's panel.
- `/health` reports per-install `pullers`, `lastPullAt`, `queued` and `exec`. "The extension
  isn't responding" used to be indistinguishable from a dead daemon, an unloaded extension,
  a sleeping worker, or a missing offscreen document. Measured with it: the extension
  re-registers its long-poll 159ms after a daemon restart, and round-trips run 3-7ms.

## 0.6.0

- Version is now verifiable at runtime, not just declared: the panel footer shows it, and
  a `version` tool reports what Chrome actually has loaded. Twice during 0.5 development a
  stale process was mistaken for a code bug; this makes that a glance instead of an
  investigation.
- `test_version.sh` fails if the manifest version has no changelog entry, or if the loaded
  extension is behind what's on disk.
- The terminal survives a daemon restart. 0.4.0's `reconnect=1` could never have fixed this:
  a restart destroys the ttyd server, so the client retries a socket that no longer has
  anything on the other end and parks on "Press ⏎ to Reconnect". `/health` now returns
  `startedAt`; the panel watches it, and re-attaches when it changes. tmux still holds the
  session, so the conversation resumes where it left off. It probes ttyd before attaching,
  since ttyd can lag the bridge and attaching early would leave a dead error page.
- Picker closes on click-away again. A document click handler missed the most common case:
  clicking the terminal lands inside the iframe, which swallows the event, so the menu stayed
  open over it. Window blur covers that; Escape closes it too.

## 0.5.0

- Added the `storage` permission. The worker's state pattern was copied from apple-a-day
  without it, so every read threw `Cannot read properties of undefined (reading 'local')`.
- tmux field separator changed from `\t` to `|`. Without a controlling terminal — which is
  how the daemon actually runs, under launchd — tmux sanitizes control characters in list
  output to `_`, collapsing the separator into the values so every `/sessions` row parsed
  as `tabId: null`. Invisible in interactive testing, where tmux has a tty.

## 0.4.0

- LaunchAgent (`install-daemon.sh`) runs bridge + ttyd with `KeepAlive`, so `kill -9`,
  crash, logout, and reboot all end with it back up in seconds.
- `offscreen.js` owns the long-poll instead of the service worker. Offscreen documents have
  no idle timer; the worker is killed at ~30s idle, which was the real cause of tools
  "disconnecting" whenever you stopped typing. It wakes the worker by messaging it, which
  is precisely the event that revives a dead worker.
- ttyd runs with `reconnect=1` and `disableLeaveAlert=true` — a dropped socket retries in a
  second instead of parking on "Press ⏎ to Reconnect", and never prompts "Leave site?".
- Added `refresh` and `reload_extension` tools.

## 0.3.0

- Tab switches no longer reload the iframe. Reassigning `src` dropped ttyd's websocket and
  fired its `beforeunload`, so every tab change prompted "Leave site?". Switching is now a
  `tmux switch-client` on the same live client.
- Session picker under the top bar: blue dot = finished while you were away, pulsing green
  = still working. Replaced a native `<select>`, which can't color individual options.
- `<meta charset="utf-8">` — its absence was rendering the dots as mojibake.
- Tab lifecycle tracked in the worker and persisted to `chrome.storage.local` (pattern from
  apple-a-day), plus a toolbar badge counting sessions that finished unseen.

## 0.2.0

- Renamed from `sidebar` to Fleetkick.
- Own tab-control MCP (`mcp.js` → `bridge.js` → extension) instead of depending on
  claude-in-chrome: `read`, `click`, `type`, `navigate`, `screenshot`, `tab_create`,
  `tabs_list`.
- Per-tab tmux sessions, so every tab keeps its own warm conversation.

## 0.1.0

- Chrome side panel running a real `claude` over ttyd, booted knowing its tab.
