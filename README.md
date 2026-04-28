# tmux-mcp

> An MCP server that attaches to and drives **existing** tmux sessions. Lists live sessions, sends keystrokes, captures pane content, runs commands. Maintains a registry of named sessions with metadata so an agent can refer to them by friendly name.

## What it does

Six MCP tools for operating on tmux sessions:

| Tool | What it does |
|---|---|
| `tmux_sessions()` | List all live tmux sessions on the host, plus the registered-name registry (with stale flagging) |
| `tmux_register(name, session, description?, tags?)` | Save a friendly name → underlying tmux session mapping with metadata |
| `tmux_unregister(name)` | Remove a registry entry. Does NOT touch tmux itself |
| `tmux_send(target, keys, enter=True, by_registry_name=False)` | Send keystrokes to a session |
| `tmux_capture(target, lines=200, by_registry_name=False)` | Read the visible pane + scrollback |
| `tmux_run(target, command, wait_seconds=2.0, capture_lines=200, by_registry_name=False)` | Convenience: send + wait + capture in one call |

## Why it exists

Two motivating problems:

**1. Round-trip testing of marketplace plugins.** When `eidos-marketplace` adds a plugin, verifying the install path requires a fresh Claude Code session. Doing this manually breaks autonomous workflows. Running `claude plugins install` in a known tmux session lets an agent see the result.

**2. Agent-driven session steering.** An agent in one Claude Code session may need to inspect, prompt, or steer a Claude Code (or any other) session running in another tmux pane — for handoff, for debate, for dogfooding, for monitoring a long-running task. tmux-mcp gives that capability without the agent owning the session lifecycle.

## Design principles

- **Existing sessions only.** Never spawns new sessions, never kills them. The user owns the session lifecycle; this MCP just observes and drives.
- **Registry is metadata only.** Live state always comes from `tmux list-sessions`. If a registered session no longer exists, the registry entry is marked `stale: true` but not auto-deleted — the user decides whether to re-register or unregister.
- **Best-effort capture.** tmux output may include ANSI escapes; the caller is responsible for parsing if they need clean text.
- **No magic, no recursion guards.** If you `tmux_send` a `claude` invocation into a pane that already has a Claude Code session running tmux-mcp, you get the recursion you asked for. Be deliberate.

## Install

Via uvx (no pre-install):

```bash
uvx --from tmux-mcp tmux-mcp
```

In a Claude Code marketplace plugin, the `.mcp.json` looks like:

```json
{"tmux-mcp": {"command": "uvx", "args": ["--from", "tmux-mcp", "tmux-mcp"]}}
```

Local development:

```bash
git clone https://github.com/eidos-agi/tmux-mcp
cd tmux-mcp
uv sync
uv pip install -e ".[dev]"
uv run pytest
```

## Registry storage

The registry is a JSON file at `~/.config/tmux-mcp/registry.json` (override with `$TMUX_MCP_REGISTRY`). Format:

```json
{
  "claude-prod": {
    "session": "main",
    "description": "production claude session",
    "tags": ["prod", "claude"],
    "registered_at": 1777399684
  },
  "test-shell": {
    "session": "scratch",
    "description": "scratch tmux for testing installs",
    "tags": ["test"],
    "registered_at": 1777399700
  }
}
```

Hand-edit it if you want; the format is stable.

## Example

```python
# Register a session you've created externally
await tmux_register(
    name="claude-prod",
    session="main",  # the actual tmux session name
    description="production claude session",
    tags=["prod", "claude"],
)

# Drive it
await tmux_send(target="claude-prod", keys="claude plugins list", by_registry_name=True)
await asyncio.sleep(1)
result = await tmux_capture(target="claude-prod", by_registry_name=True)
print(result["content"])

# Or in one shot
result = await tmux_run(
    target="claude-prod",
    command="claude plugins marketplace update eidos-marketplace",
    wait_seconds=3,
    by_registry_name=True,
)
```

## What it does NOT do

- **Doesn't spawn tmux sessions.** Use `tmux new-session` yourself; this MCP is read/drive only.
- **Doesn't strip ANSI.** Capture content includes the raw bytes from tmux. Strip with `re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)` if you need clean output.
- **Doesn't proxy MCP from inside the tmux session.** If the tmux session is running its own MCP server, this tool only sees stdin/stdout text — not the structured MCP messages.
- **Doesn't long-poll.** `tmux_run`'s `wait_seconds` is a fixed sleep. For commands that may take a while, prefer `tmux_send` followed by polling `tmux_capture` until you see the prompt return.

## License

MIT — see [LICENSE](LICENSE).
