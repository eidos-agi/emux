# Fleetkick

Sidekick for the fleet: a real `claude` session in a Chrome side panel, paired with the tab you're looking at, able to **see and control** it.

## Shape

```
side panel  ──iframe──>  ttyd :7681  ──>  boot.sh  ──>  tmux fleetkick-tab-<id>  ──>  claude
                                                                                        │ fleetkick MCP (mcp.js)
                                                                                        ▼
extension background.js  <──long-poll──  bridge :7682  <──POST /cmd──────────────────────
        │ chrome.tabs / chrome.scripting
        ▼
   the actual tab
```

Two things make it work:

- **Per-tab tmux.** Each tabId gets `tmux new-session -A -s fleetkick-tab-<id>`. Switch tabs and the panel just re-attaches to that tab's session — the conversation is still there, warm, with its context intact. Switch back and it's exactly where you left it.
- **Its own control tools.** `mcp.js` is a stdio MCP server the session gets via `--mcp-config`, proxying to `bridge.js`, which the extension long-polls. The extension executes with `chrome.tabs` / `chrome.scripting` and posts the result back. Tools: `read`, `click`, `type`, `navigate`, `screenshot`, `tab_create`, `tabs_list`. They target the paired tab by default; pass `tabId` to steer any other tab.

The paired session is told to use `fleetkick` tools for the browser and `emux` tools for anything terminal-side, so it doesn't reach for claude-in-chrome.

## Run

1. `./serve.sh [workdir]` — starts the bridge and ttyd (needs `brew install ttyd`).
2. `chrome://extensions` → Developer mode → **Load unpacked** → `fleetkick/extension/`.
3. Click the toolbar icon → panel opens, paired with the current tab.

## Proof it's paired

The top bar shows the favicon, title, and URL; the bottom bar shows `tab <id> · window <id>` — all read from the same `chrome.tabs` object that booted the session. Ask it *"what tab are you on?"* and it answers from its system prompt with no tool call, then `read` confirms it live. Switch tabs and both bars plus the terminal follow.

## Check

`./test_roundtrip.sh` — drives mcp.js → bridge → a fake extension and back without Chrome, asserting the paired tabId actually rides along and that a web origin gets 403.

## Security

Both ports bind 127.0.0.1 only. ttyd runs with `-O` (origin-checked websockets), and the bridge rejects any request carrying an `http(s)` `Origin` header, so a random page you visit can't spawn a shell or drive your tabs.
