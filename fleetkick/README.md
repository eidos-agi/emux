# Fleetkick

Sidekick for the fleet: a real `claude` session in a Chrome side panel, paired with the tab you're looking at, able to **see and control** it.

## Shape

```
side panel ──iframe──> ttyd :7681 ──> boot.sh ──> tmux fleetkick-tab-<id> ──> claude
                                                                               │ fleetkick MCP (mcp.js)
                                                                               ▼
offscreen.js ──long-poll──> bridge :7682 <──POST /cmd───────────────────────────
     │ wakes the worker with a message
     ▼
background.js ──chrome.tabs / chrome.scripting──> the actual tab
```

Nothing here assumes anything stays running, because in this stack almost nothing does.

- **The daemon can't be shut down.** `bridge.js` + `ttyd` run under a LaunchAgent with `KeepAlive`, so `kill -9`, a crash, logout, or a reboot all end the same way: back up within seconds. Everything else in the system is free to reconnect whenever it likes, because the thing it reconnects to is always there.
- **The connection is never the service worker's job.** MV3 kills the worker after ~30s idle, and a dead worker stops pulling commands — that's the "tools disconnect when I stop typing" bug. `offscreen.js` holds the long-poll instead (offscreen documents have no idle timer) and wakes the worker with a message when work arrives; delivering that message *is* the event that revives it. The offscreen doc is the wire, the worker is the hands.
- **The terminal reconnects instead of giving up.** ttyd runs with `reconnect=1` and `disableLeaveAlert=true`, so a dropped socket retries in a second rather than parking on "Press ⏎ to Reconnect", and it never asks "Leave site?". tmux is holding the session, so a reconnect lands back in the same conversation.
- **Per-tab tmux.** Each tabId gets `tmux new-session -A -s fleetkick-tab-<id>`. Switching tabs never reloads the iframe (that's what fired the "Leave site?" prompt) — it's a `tmux switch-client` on the same live client. Every tab keeps its own warm conversation, context intact.
- **State lives in `chrome.storage.local`, written by the worker.** Borrowed from apple-a-day's browser monitor: the worker is killed constantly and the panel is usually closed, so panel-only state means going blind exactly when you should be watching.

## Tools the session gets

`read`, `click`, `type`, `navigate`, `refresh`, `screenshot`, `tab_create`, `tabs_list`, `reload_extension`. With no `tabId` they act on the **selected** tab, so the session follows you as you switch; pass `tabId` (from `tabs_list`, which sees every tab in every window) to act on any other one. Set `FLEETKICK_TAB` to pin a whole session to one tab.

## Run

1. `./install-daemon.sh [workdir]` — installs and starts the LaunchAgent. (Or `./serve.sh` to run it in the foreground.)
2. `chrome://extensions` → Developer mode → **Load unpacked** → `fleetkick/extension/`.
3. Click the toolbar icon → the panel opens, paired with the current tab.

Uninstall the daemon: `launchctl bootout gui/$UID/com.eidos.fleetkick && rm ~/Library/LaunchAgents/com.eidos.fleetkick.plist`.

## The picker

Under the top bar, a dropdown of every session. A blue dot means that session produced output after you last had it on screen and has since gone quiet — it finished something while you were away. Pulsing green means it's still working. The toolbar badge shows the blue count, so you can see it without opening the panel at all. Picking a session switches the terminal to it and focuses its Chrome tab, so the picker and the tools never disagree about what "this tab" means.

## Checks

- `./test_roundtrip.sh` — drives mcp.js → bridge → a fake extension and back without Chrome. Asserts an explicit tabId rides along, an unpinned call sends none (so the extension resolves the selected tab), a non-numeric tabId is rejected before it reaches tmux, and a web origin gets 403.
- `./test_daemon.sh` — `kill -9`s both ports and asserts launchd rebuilds them with a new pid.

## Security

Both ports bind 127.0.0.1 only. ttyd runs with `-O` (origin-checked websockets). The bridge refuses any request carrying an `http(s)` `Origin`, and separately requires an `x-fleetkick` header — a page can't set that without a preflight, and its preflight is refused by the Origin check, so no site you visit can spawn a shell or drive your tabs. tabIds are digits-only checked before they reach tmux, and tmux is driven through `execFile` argv, never a shell.
