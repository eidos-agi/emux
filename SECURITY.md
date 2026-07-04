# Security

Emux can send keystrokes to existing tmux sessions and capture pane output. Treat that as a powerful local-control surface.

## Supported Versions

Only the current `main` branch is supported before the first tagged release.

## Reporting

Report security issues privately to hello@eidosagi.com.

## Threat Model

- Emux never spawns or kills tmux sessions.
- Emux does not store captured terminal output in its registry.
- The registry contains only session metadata and lives at `~/.config/emux/registry.json` unless `EMUX_REGISTRY` is set.
- The MCP server trusts the local user who installed and launched it. Do not expose it over a network transport.
- Captured pane output may contain secrets already visible in the terminal. Callers must avoid logging or sharing captured content blindly.
