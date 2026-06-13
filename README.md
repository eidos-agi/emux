# emux

> **Eidos mux.** Pick up where you left off in tmux. A TUI session picker for humans + an MCP server for agents — same registry, same sessions, same operating model.

## What it does

Three front-ends over one shared registry of named tmux sessions:

```
emux              → TUI picker. Lists registered + live sessions.
                    Pick one → tmux attach. Stale entries flagged.

emux mcp          → MCP server. Six tools for agents to drive
                    sessions: list, register, send, capture, run.

emux web          → Web daemon. Browser UI that monitors any session
                    like a chatbot: live pane is the bot's side of
                    the chat, input bar types into the session.

emux ls           → Print registered + live sessions (non-interactive,
                    CI-friendly).
emux register     → Register a session under a friendly name.
emux unregister   → Drop a registered name. Doesn't touch tmux.
```

The registry persists at `~/.config/emux/registry.json` (override via `$EMUX_REGISTRY`).

## Why it exists

Two motivating problems, one tool:

**For humans:** "Which tmux session was I working in?" After ten sessions accumulate, remembering which one had the long-running build, which one had the Claude Code chat with useful context, which one was a throwaway — that's the friction. emux's TUI shows the registered names with descriptions ("production claude session", "test-shell", "long backfill") and stale flags (sessions you registered but tmux has since reaped). Pick one, you're attached. No remembering tmux session ids.

**For agents:** When an agent in one Claude Code session needs to inspect, prompt, or steer a session running in another tmux pane — for handoff, debate, monitoring, or autonomous round-trip testing of marketplace installs — it needs structured access to send keys and read the result. emux's MCP server gives that without the agent owning session lifecycle.

The registry is the same surface for both. Register once interactively, drive forever from agents. Or vice versa.

## Install

Via uvx (no pre-install):

```bash
uvx --from emux emux                  # TUI picker
uvx --from emux emux mcp              # MCP server
```

In a Claude Code marketplace plugin, the `.mcp.json` looks like:

```json
{"emux": {"command": "uvx", "args": ["--from", "emux", "emux", "mcp"]}}
```

Local development:

```bash
git clone https://github.com/eidos-agi/emux
cd emux
uv sync
uv pip install -e ".[dev]"
uv run pytest
```

## TUI picker

Running `emux` with no arguments opens a numbered list of choices:

```
emux v0.1.0 — pick a session to attach

   1  claude-prod   → main           live    — production claude session  #prod #claude
   2  test-shell    → scratch        live    — scratch tmux for testing   #test
   3  long-build    → backfill       STALE — tmux session gone   — overnight ETL run
   4  experiments   unregistered live tmux session
   5  (register new)  register a new session by typing name + tmux session id

  pick [1-5], or q to quit:
```

- **Registered + live** entries attach immediately on selection (`tmux attach -t <session>`).
- **Stale** registered entries explain that the underlying tmux session is gone; you can pick again, unregister it, or re-register against a live session.
- **Live but unregistered** entries offer to register them inline before attaching.
- **(register new)** prompts for `name`, `session id`, optional `description`, and tags, then optionally attaches.

The TUI is intentionally minimal: stdlib `input()`, no external TUI library. Works in any terminal, including remote SSH, dumb terminals, and CI shells.

## MCP server

Six tools, exposed via `emux mcp`:

| Tool | What it does |
|---|---|
| `tmux_sessions()` | List live tmux sessions + registry (with stale flag) |
| `tmux_register(name, session, description?, tags?)` | Save friendly-name → session mapping with metadata |
| `tmux_unregister(name)` | Remove from registry; doesn't touch tmux |
| `tmux_send(target, keys, enter, by_registry_name)` | Send keystrokes |
| `tmux_capture(target, lines, by_registry_name)` | Read pane + scrollback |
| `tmux_run(target, command, wait_seconds, ...)` | Convenience: send + sleep + capture |

Example: agent drives a registered session.

```python
await tmux_register(
    name="claude-prod",
    session="main",
    description="production claude session",
    tags=["prod", "claude"],
)

result = await tmux_run(
    target="claude-prod",
    command="claude plugins marketplace update eidos-marketplace",
    wait_seconds=3,
    by_registry_name=True,
)
print(result["content"])  # tmux pane contents after the command
```

## Web daemon

`emux web` starts a persistent local HTTP server with monitoring + chat views:

```bash
emux web                  # http://127.0.0.1:8689
emux web --port 9000 --open
```

Five views over the same registry:

- **Grid** — every session as a live mini-pane tile, all streaming at once (2s poll). Tiles glow when their pane changed in the last few seconds; click one to drop into chat.
- **Groups** — the same tiles sectioned by registry tag (`#prod`, `#agents`, …), with `untagged` and `unregistered` sections at the end. A session with multiple tags appears in each of its groups.
- **Activity** — one row per session with a 60-sample change-detection strip (lit cell = the pane moved during that sample) and a "last active" age. Detection ignores cursor blinks and spinner frames (braille/block glyphs are stripped before comparison) so an idle session with a thinking spinner doesn't read as busy. Tracking lives in the daemon, so every browser tab sees the same history.
- **Flow** — agent topology as a layered hierarchy: orchestrators on top, the agents they drive below, connected by directed **manages** arrows. Each node is a **live mini tmux pane** with a title bar showing the session name and the **detected AI/tool** running in it — Claude Code (✳), Codex (◇), Gemini (♊), Hermes (☿), Aider (✦), or the raw process name otherwise — so you watch the whole fleet working at once. Detection reads tmux's live `pane_current_command`, falling back to a content signature for node-wrapped CLIs that all report as `node`. Built from registry relationships (`emux register boss main --manages worker-1 worker-2`, or the `manages` arg on the MCP `tmux_register` tool); sessions in no relationship sit in an "unconnected" row at the bottom. (Edges reflect *declared* intent in the registry, not observed traffic.)
- **Chat** — pick any session (sidebar or any tile/node). Its pane renders as a **live screen that updates in place** — it's the rendered terminal, so a full-screen TUI like Claude Code or vim mutates rather than scrolls — with your keystrokes logged as a chat above it. The input bar sends what you type into the session verbatim (`send-keys -l` + Enter); control chips (`^C`, `ESC`, `⏎`, `↑`, `TAB`) send named keys for steering interactive programs.

One background thread captures every live pane on a timer into a shared cache, so N tabs watching M sessions cost one capture sweep, not N×M; dead sessions are evicted from the cache as tmux reaps them.

**Niceties:** keys `1`–`4` switch views and `Esc` leaves chat; the last view is remembered across reloads; a sidebar **filter** narrows by name; tile/row ages are color-tiered by recency; sessions show **uptime** and an **attached** marker; the tab title shows the live count (and flashes when a watched chat session changes in the background); polling pauses on a hidden tab. A wrap toggle, copy-attach button, and per-message timestamps live in the chat view.

API: `GET /healthz` (unauthenticated liveness), `GET /api/sessions`, `GET /api/grid?lines=` (captures + activity for all live panes in one call), `GET /api/capture?session=&lines=`, `POST /api/send {session, keys, literal, enter}`. The `/api/*` routes enforce the Host/Origin guards above. Same operations the MCP server exposes, over HTTP.

### Security

Localhost is **not** a security boundary — any web page open in your browser can issue requests to a localhost port. So the API:

- rejects `/api/*` requests whose `Host` header isn't a loopback name (DNS-rebinding defense), and
- rejects `POST /api/send` carrying a cross-origin `Origin` header (CSRF defense — a forged keystroke-injection request from another tab).

There is still **no authentication**. Keep the bind on `127.0.0.1`; only use `--host` on a network you fully trust.

### Running it as a real service

`emux web` backgrounded by hand dies on logout/reboot. To keep it running, install the generated launchd agent (macOS):

```bash
emux web --print-launchd > ~/Library/LaunchAgents/com.eidos.emux-web.plist
launchctl load ~/Library/LaunchAgents/com.eidos.emux-web.plist
```

It sets `RunAtLoad` + `KeepAlive`, logging to `/tmp/emux-web{,.err}.log`.

**Security:** binds `127.0.0.1` and has **no auth** — anything that can reach the port can type into your tmux sessions. `--host 0.0.0.0` prints a warning; only do it on a network you trust end to end.

## Design principles

- **Existing sessions only.** Never spawns, never kills tmux sessions. Lifecycle is the user's. emux just observes and drives.
- **Registry is metadata only.** Live state always comes from `tmux list-sessions`. Stale entries are flagged, not auto-deleted — the user decides.
- **One registry for both surfaces.** TUI and MCP read and write the same JSON. Register interactively, drive from an agent. Or the reverse.
- **Stdlib TUI.** No `prompt_toolkit`, no `textual`, no `rich`. The picker is `input()` + a numbered list. Keeps install footprint tiny and works in every terminal.
- **No magic, no recursion guards.** Sending `claude` keystrokes into a session that's already running emux's MCP gives you the recursion you asked for. Be deliberate.

## Storage

Registry JSON at `~/.config/emux/registry.json` (override via `$EMUX_REGISTRY`). Format:

```json
{
  "claude-prod": {
    "session": "main",
    "description": "production claude session",
    "tags": ["prod", "claude"],
    "registered_at": 1777400000
  }
}
```

For backwards compatibility with the prior name (`tmux-mcp`), `$TMUX_MCP_REGISTRY` is also honored if `$EMUX_REGISTRY` is unset.

## What it does NOT do

- **Doesn't spawn tmux sessions.** Use `tmux new-session` yourself; emux is read/drive only.
- **Doesn't strip ANSI.** Capture content includes raw bytes from tmux. Strip with `re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)` if you need clean output.
- **Doesn't proxy MCP from inside tmux.** If the tmux session is running its own MCP server, emux only sees the stdin/stdout text — not the structured MCP messages.
- **Doesn't long-poll.** `tmux_run`'s `wait_seconds` is a fixed sleep. For long commands, prefer `tmux_send` + polling `tmux_capture` until you see the prompt return.

## License

MIT — see [LICENSE](LICENSE).
