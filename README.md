# claude-runner

> An MCP server that runs `claude` CLI commands as subprocesses. For agents who need to autonomously test marketplace installs, run scripted Claude invocations, or verify CLI behavior without a human in the loop.

## What it does

Exposes one MCP tool, `claude_run`, that spawns the `claude` Code CLI as a subprocess, captures stdout/stderr/returncode, and returns a structured JSON result. Optionally refreshes a marketplace cache before running the main command.

This is **Tier 1**: one-shot, capturable commands. No PTY. No tmux. No interactive driving. (Tier 2 — interactive driving via tmux — is deferred until a use case demands it.)

## Why it exists

When the `eidos-marketplace` adds a new plugin, the round-trip install test (`claude plugins install <plugin>@<marketplace>`) needs a fresh Claude Code session to verify. Doing this manually breaks the agent-driven workflow. `claude-runner` lets an agent run that test itself and observe the result.

It also surfaces a common, silent failure: when a marketplace's local cache is stale, `marketplace add` succeeds but `install` fails with `Plugin not found`. `claude-runner`'s `refresh_marketplace` parameter runs `claude plugins marketplace update <name>` first, so cache staleness can't silently break tests.

## Install

Via uvx (no pre-install needed):

```bash
uvx --from claude-runner claude-runner
```

In a Claude Code marketplace plugin, the `.mcp.json` looks like:

```json
{"claude-runner": {"command": "uvx", "args": ["--from", "claude-runner", "claude-runner"]}}
```

For local development:

```bash
git clone https://github.com/eidos-agi/claude-runner
cd claude-runner
uv sync
```

## Usage

The MCP exposes one tool:

```
claude_run(
  args: list[str],                     # arguments passed to `claude` (e.g. ["plugins", "install", "cept@eidos-marketplace"])
  timeout: int = 300,                  # seconds; first uvx fetch can be slow, allow headroom
  refresh_marketplace: str | None = None,  # if set, runs `claude plugins marketplace update <name>` first
  cwd: str | None = None,              # working directory for the subprocess
) -> dict
```

Returns a JSON-serializable dict:

- On success: `{"ok": true, "returncode": 0, "stdout": "...", "stderr": "...", "elapsed_seconds": 1.7}`
- On non-zero exit: `{"ok": false, "returncode": 1, "stdout": "...", "stderr": "...", "elapsed_seconds": 0.3}`
- On timeout: `{"ok": false, "error": "timeout", "stdout_partial": "...", "stderr_partial": "...", "hint": "..."}`
- On missing `claude` binary: `{"ok": false, "error": "command_not_found", "hint": "..."}`

When `refresh_marketplace` is set, the result also includes a `refresh: {ok, elapsed_seconds}` block.

### Example: verify a marketplace plugin install

```python
result = await claude_run(
    args=["plugins", "install", "cept@eidos-marketplace"],
    refresh_marketplace="eidos-marketplace",
    timeout=180,
)
assert result["ok"], result
```

This refreshes the marketplace cache, then runs the install, then returns the structured result.

## What it does NOT do

- **No interactive sessions.** It cannot drive a long-running `claude` REPL, send slash commands mid-session, or capture streaming output. That's Tier 2 (deferred).
- **No PTY emulation.** Output is captured via `subprocess.run`'s pipes. ANSI sequences in the output are passed through untouched. If you need a terminal-rendered view, that's also Tier 2.
- **No recursion guards.** If you run `claude_run(args=["chat"])` from inside a Claude Code session that has `claude-runner` loaded, the child session also has `claude-runner`. Be deliberate about this.

## License

MIT — see [LICENSE](LICENSE).
