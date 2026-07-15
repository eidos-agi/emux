# emux

> **Eidos mux.** Pick up where you left off in tmux, and let an agent drive it. A TUI session picker for humans + an MCP server for agents to observe, converse with, navigate, and autonomously pursue goals through existing sessions — never spawning or killing them. Same registry, same sessions, same operating model.

## What it does

Three front-ends over one shared registry of named tmux sessions:

```
emux              → TUI picker. Lists registered + live sessions.
                    Pick one → tmux attach. Stale entries flagged.

emux mcp          → MCP server. Tools for agents to drive sessions:
                    list, register, send, capture, run — plus the
                    drive tiers: ask, navigate, goal (see below).

emux ask          → Send a prompt to an AI in a session, wait for its
                    reply to settle, print it.
emux navigate     → Model-driven: reach a target screen through a TUI.
emux goal         → Autonomous: pursue a whole task through a TUI.
emux login        → Drive a Claude Code login in a session: surface the
                    OAuth URL, then finish with --code. --switch to change
                    account (/logout first).

emux web          → Web daemon. Browser UI that monitors any session
                    like a chatbot: live pane is the bot's side of
                    the chat, input bar types into the session.

emux web          → Web daemon. Browser UI that monitors any session
                    like a chatbot: live pane is the bot's side of
                    the chat, input bar types into the session.

emux ls           → Print registered + live sessions (non-interactive,
                    CI-friendly).
emux watch        → Watch many registered/live sessions in one terminal.
emux send         → Send keys or text to a registered/live session.
emux interrupt    → Send C-c to a registered/live session.
emux capture      → Capture pane output from a registered/live session.
emux run          → Send a command, wait, and capture output.
emux head         → Open a real terminal head attached to a session
                    (remote sessions attach via ssh -t).
emux doctor       → Diagnose a session's ENVIRONMENT: liveness, capture,
                    stream log, gate, and tmux-server vs fresh-process
                    filesystem access (finds the macOS TCC lockout).
emux gates        → Mine the gate ledger: which gates recur, how they were
                    answered, ready-to-paste auto-answer policy rules.
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

Until the first PyPI release, run directly from Git or from a local checkout:

```bash
uvx --from git+https://github.com/eidos-agi/emux.git emux       # TUI picker
uvx --from git+https://github.com/eidos-agi/emux.git emux mcp   # MCP server
```

In a Claude Code marketplace plugin, the `.mcp.json` looks like:

```json
{"emux": {"command": "uv", "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "emux", "mcp"]}}
```

Local development:

```bash
git clone https://github.com/eidos-agi/emux
cd emux
uv sync
uv pip install -e ".[dev]"
uv run pytest
uv run ruff check .
```

## TUI picker

Running `emux` with no arguments opens a Textual picker with a filter box,
number-key shortcuts, grouped session lists, and a live preview pane:

```
Registered (live)
   1  ●  claude-prod  → main

Registered (stale)
   2  ●  long-build  → backfill

Unregistered live tmux
   3  ○  experiments  unregistered

Actions
   4  ✦  (new mission)
   5  ⊕  (register new)
```

- **Registered + live** entries attach immediately on selection (`tmux attach -t <session>`).
- **Stale** registered entries warn that the underlying tmux session is gone; they do not attach.
- **Live but unregistered** entries attach on Enter and can be registered with `r`.
- **(register new)** prompts for `name`, `session id`, optional `description`, and tags, then optionally attaches.

The picker is a terminal UI, not a terminal owner. Sessions are registered with
Emux for discovery and attached via Emux when selected. tmux still owns the
session lifecycle.

## New mission (`Ctrl-N` / `emux new`)

Describe what you want in plain English; an AI turns it into a session spec
you confirm before anything runs:

```
  what do you want to do? check on dally on my mac-mini

  ━━ mission plan ━━
  summary   Check the status of "dally" on the Mac mini and report back.
  name      check-dally
  host      Daniels-Mac-mini.local
  cwd       (default)
  command   claude 'Check on "dally" …' --permission-mode bypassPermissions
  perms     bypassPermissions (fully unattended — no approval prompts)
  path      this terminal → ssh Daniels-Mac-mini.local → tmux new-session 'check-dally' → claude
  attach    ssh -t Daniels-Mac-mini.local 'tmux attach -t check-dally'

  start it? [Y/n, or type changes]:
```

- The planner runs on `claude -p` (fixed-cost; model via `$EMUX_PLAN_MODEL`,
  default sonnet) and knows the registry's remote hosts, so "on my mac-mini"
  resolves to a real ssh destination. It asks at most one clarifying question
  per turn; free-text at the confirm prompt refines the plan instead of
  starting it.
- The `path` line is the hop chain, derived from the plan fields — never model
  prose. `perms` is picked by the planner (default / acceptEdits /
  bypassPermissions) and appended to claude commands by emux itself.
- On confirm, the mission spawns via `tmux_spawn` (local or remote), registers
  under the mission name, prints **first light** — the pane after 2.5s, so a
  dead launch (claude not on PATH, not logged in, folder-trust dialog) is
  caught immediately — and offers to attach.
- If the mission name is already a live session, emux never silently kills it:
  unique-suffix by default (`check-dally-2`), replace only on request.

Same flow from the shell: `emux new`. In the TUI, `Ctrl-N` works from anywhere
(plain `n` too, once the session list has focus).

## MCP server

Tools exposed via `emux mcp`:

| Tool | What it does |
|---|---|
| `tmux_sessions()` | List live tmux sessions + registry (with stale flag) |
| `tmux_register(name, session, description?, tags?)` | Save friendly-name → session mapping with metadata |
| `tmux_unregister(name)` | Remove from registry; doesn't touch tmux |
| `tmux_send(target, keys, enter, by_registry_name)` | Send keystrokes |
| `tmux_capture(target, lines, by_registry_name)` | Read pane + scrollback |
| `tmux_run(target, command, wait_seconds, ...)` | Convenience: send + sleep + capture |
| `tmux_ask(target, prompt, ...)` | Send a prompt, wait for the reply to **settle**, return it |
| `tmux_navigate(target, goal, until?, ...)` | Model-driven: reach a target screen through a TUI |
| `tmux_goal(target, goal, ...)` | Autonomous: pursue a whole task through a TUI |
| `tmux_login(target, code?, switch?, ...)` | Drive a Claude Code login sequence; surfaces the OAuth URL |
| `tmux_doctor(target, ...)` | Diagnose a session's environment (incl. tmux-server vs fresh-process fs access) |

### Driving another AI through its TUI

The last three tools are a ladder of increasing autonomy over an AI (or any
interactive program) running in a session — `railway.new`'s agent, a
`claude`/`codex`/`aider` REPL, an installer:

| Tier | Reaches | Intelligence |
|---|---|---|
| `send` / `capture` / `run` | raw keystrokes + screen | none (mechanical) |
| **`ask`** | a settled reply | dumb settle-timer — waits until the pane stops changing (a fixed sleep can't, since a reply streams for an unknown time) |
| **`navigate`** | a target *screen* | a model reads each screen and picks keystrokes toward a stated goal |
| **`goal`** | a whole *task done* | an autonomous observe → act → judge loop, with recovery |

**Recovery** (`navigate` / `goal`): escalates the model Haiku→Sonnet on a stall,
retries a transient blank/stall capture, detects a stuck loop, and aborts
cleanly if the session dies (`session_gone`) instead of flailing.

**Destructive-action gate** (`navigate` / `goal`, on by default): the run is
blocked (`blocked_dangerous`) if a step would type a destructive command
(`rm -rf`, `DROP TABLE`, force-push…) or confirm a destructive on-screen prompt
("Delete? [y]"). It's a heuristic denylist, not a sandbox — disable with `--yolo`
(or `$EMUX_ALLOW_DANGEROUS`) when you know the surface is safe.

**Drift-guard** (`goal --telos` / `tmux_goal(telos=True)`): route an autonomous
run through [telos-md](https://github.com/eidos-agi/telos.md). emux opens a telos
north star for the goal, **ticks it every step**, and **aborts (`telos_stop`) if
telos signals drift or no-progress** — an independent conscience over the loop.
Every run is also *recorded* (north star + ticks + close: reached/abandoned) in
one telos home (`$EMUX_TELOS_HOME`, default `~/.local/share/emux/telos`), so
`telos-md traffic --repo-path <that>` shows every autonomous run emux has driven.
Opt-in and best-effort — if `telos-md` isn't on `PATH` the loop just runs
unguarded. (Also enabled by `$EMUX_TELOS=1`.)

> **Requires the `claude` CLI on `PATH`** — `navigate` and `goal` make model
> calls via `claude -p` (a fixed-cost subscription tool, never the raw API).
> `send`/`capture`/`run`/`ask` need only tmux. Tune the models with
> `$EMUX_NAV_MODEL` (default Haiku) and `$EMUX_NAV_MODEL_ESCALATE` (default Sonnet).

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

### Login gates (`emux login` / `tmux_login`)

A managed session that logs out (or is on the wrong account) dead-ends
supervision: the classifier reports `waiting_human` with a **`login_gate`**
flag, and nothing can proceed until the OAuth hop happens in a browser. `emux
login` drives everything except that hop:

```
emux login worker-3 -n              → sends /login, steps the TUI, prints the
                                      OAuth URL to open in a browser
emux login worker-3 -n --code <c>   → pastes the authorization code, verifies
                                      "Login successful" on screen
emux login worker-3 -n --switch     → change account: /logout first, then above
```

Deterministic keystrokes only — the login TUI is a fixed sequence, so no model
calls are needed and it works on remote sessions via the registry's host. The
code is sent as keystrokes and never persisted (redacted from the audit trail).
Details: `docs/emux-login.md`.

## Web daemon

`emux web` starts a persistent local HTTP server with monitoring + chat views:

```bash
emux web                  # http://127.0.0.1:8689
emux web --port 9000 --open
```

### Gate policy — answer known gates without a human or a model

When the daemon sees a worker blocked on a modal gate, it first consults
`~/.config/emux/gatepolicy.json` (override via `$EMUX_GATE_POLICY`):

```json
{"rules": [
  {"pattern": "trust this folder", "keys": ["Enter"], "note": "folder-trust gate"}
]}
```

A rule whose regex matches the pane's live bottom answers the gate with those
exact keystrokes — deterministic, host-aware, zero tokens. Guardrails: gates
showing destructive text (`rm -rf`, force-push, "permanently delete", …) are
NEVER auto-answered regardless of rules, and each gate gets ONE attempt — if
the same gate is still up next poll it escalates to a human (NEED signal +
auto-opened terminal head) as before. Every sighting lands in
`~/.local/share/emux/gates.jsonl`; run `emux gates` to see which gates recur,
how they were answered, and copy-paste-ready rules for them.

Five views over the same registry:

- **Grid** — every session as a live mini-pane tile, all streaming at once (2s poll). Tiles glow when their pane changed in the last few seconds; click one to drop into chat.
- **Groups** — the same tiles sectioned by registry tag (`#prod`, `#agents`, …), with `untagged` and `unregistered` sections at the end. A session with multiple tags appears in each of its groups.
- **Activity** — one row per session with a 60-sample change-detection strip (lit cell = the pane moved during that sample) and a "last active" age. Detection ignores cursor blinks and spinner frames (braille/block glyphs are stripped before comparison) so an idle session with a thinking spinner doesn't read as busy. Tracking lives in the daemon, so every browser tab sees the same history.
- **Flow** — agent topology as a layered hierarchy: orchestrators on top, the agents they drive below, connected by directed **manages** arrows. Each node is a **live mini tmux pane** with a title bar showing the session name and the **detected AI/tool** running in it — Claude Code (✳), Codex (◇), Gemini (♊), Hermes (☿), Aider (✦), or the raw process name otherwise — so you watch the whole fleet working at once. Detection reads tmux's live `pane_current_command`, falling back to a content signature for node-wrapped CLIs that all report as `node`. Built from registry relationships (`emux register boss main --manages worker-1 worker-2`, or the `manages` arg on the MCP `tmux_register` tool); sessions in no relationship sit in an "unconnected" row at the bottom. (Edges reflect *declared* intent in the registry, not observed traffic.) The panes stream live in place (the layout only rebuilds when the topology changes). **Click any box to zoom into a modal** with the full live screen and an input bar to prompt/steer that session — control chips (`^C`, `ESC`, `⏎`, `↑`, `TAB`) included; `Esc` or click-outside closes it.
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
- **Textual TUI.** The picker uses Textual for filtering, preview, keyboard shortcuts, and grouped session state.
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
- **Doesn't bypass auth or approvals.** If the controlled session asks Claude Code for login, MFA, approval, or a human decision, Emux only sees and sends terminal text.
- **Doesn't strip ANSI.** Capture content includes raw bytes from tmux. Strip with `re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)` if you need clean output.
- **Doesn't proxy MCP from inside tmux.** If the tmux session is running its own MCP server, emux only sees the stdin/stdout text — not the structured MCP messages.
- **`tmux_run` doesn't wait for streaming output.** Its `wait_seconds` is a fixed sleep — fine for a command that finishes fast. For an AI whose reply streams in over an unknown time, use `tmux_ask` (settles automatically) instead.
- **Doesn't gate destructive actions.** `navigate`/`goal` send whatever keystrokes the model chooses, including `Enter` on a confirm. Scope goals accordingly.

## Claude Code in tmux

Emux can control Claude Code when Claude Code is already running inside tmux:

```bash
tmux new -s claude-code
claude
```

From another terminal or agent, register and drive that existing session:

```bash
emux register claude-code claude-code -d "Claude Code terminal" -t claude local
```

Agents can then use `tmux_run(..., by_registry_name=True)` or separate
send/capture calls against `claude-code`.

## Watching many sessions

Use `emux watch` to watch all registered sessions plus live unregistered tmux
sessions in one refreshing terminal dashboard:

```bash
emux watch
emux watch --filter claude
emux watch --registered-only
emux watch --once --lines 12
```

This is a watcher, not a supervisor. It repeatedly captures visible pane
content with `tmux capture-pane`; it does not send input, create sessions, or
decide whether a Claude Code session is blocked.

## Controlling while watching

Keep `emux watch` running in one terminal, then use the control commands from
another terminal or agent. CLI targets are registry names by default:

```bash
emux interrupt claude-code
emux send claude-code "continue, but only run the focused test"
emux capture claude-code --lines 80
emux run claude-code "uv run pytest tests/test_basic.py -q" --wait 3 --lines 120
```

Use `--session` when you want to target a raw tmux session name instead of a
registered Emux name:

```bash
emux send --session scratch "pwd"
```

## Opening a terminal head

Use `emux head` when you want a real terminal attached to a registered session:

```bash
emux head claude-code
emux head claude-code --terminal iterm
emux head claude-code --terminal terminal
emux head claude-code --print-command
```

On macOS, `emux head` tries iTerm2/iTerm first and falls back to Terminal.app if
iTerm is unavailable or not responding. The head runs `tmux attach -t <session>`
inside the terminal app, so paste, raw keys, `Ctrl-C`, scrollback, resizing, and
Claude Code's own terminal UI stay native.

## License

MIT — see [LICENSE](LICENSE).
