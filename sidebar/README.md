# emux sidebar

A real `claude` code TUI in a Chrome side panel, booted knowing exactly which tab it's paired with.

How it works: `serve.sh` runs ttyd (localhost-only, origin-checked) serving `claude`. The extension's side panel iframes it, and on open passes `--append-system-prompt "tabId=…, url=…, title=…"` via ttyd URL args — so claude wakes up already knowing its tab and can drive it through the claude-in-chrome MCP.

## Run

1. `./serve.sh [workdir]` — starts ttyd on 127.0.0.1:7681 (needs `brew install ttyd`).
2. `chrome://extensions` → Developer mode → Load unpacked → `sidebar/extension/`.
3. Click the toolbar icon on any tab → side panel opens with claude booted for that tab.

Each panel-open spawns a fresh claude session bound to the tab that was active at that moment. Close/reopen the panel to re-pair.

<!-- ponytail: fresh spawn per open; tmux-backed sessions (emux-supervised, survive panel close,
     warm-worker) is the upgrade path once the raw loop is proven. -->

## Manual check

Open the panel on some tab and ask claude "what tab do you have access to?" — it should answer with that tab's exact tabId/url without any tool calls.
