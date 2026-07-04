# Contributing

Emux is a small Eidos AGI tool. Keep changes narrow and prove them locally.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

For live tmux behavior, create a disposable tmux session and use a temporary registry:

```bash
export EMUX_REGISTRY="$(mktemp -t emux-registry.XXXXXX.json)"
tmux new-session -d -s emux-smoke 'zsh'
uv run emux register smoke emux-smoke
uv run emux ls
tmux kill-session -t emux-smoke
rm -f "$EMUX_REGISTRY"
```

## Boundaries

- Do not make Emux spawn or kill user sessions.
- Do not store terminal contents in the registry.
- Keep the registry as metadata: friendly name, tmux session id, description, tags, timestamp.
- Prefer explicit errors over guessing when tmux is missing or a session is stale.
