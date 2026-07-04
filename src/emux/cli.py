"""emux CLI dispatcher.

  emux              → TUI picker (registered + live tmux sessions)
  emux mcp          → start the MCP server
  emux web          → start the web daemon (chat-style session monitor)
  emux register …   → CLI register
  emux ls           → list registered + live sessions
  emux --version    → print version

The TUI is intentionally minimal: stdlib only, numbered prompt. The picker
shows registered sessions first (with description and stale flag), then
live-but-unregistered sessions, then offers to register a new entry. On
selection, exec `tmux attach -t <session>` so the user lands in the actual
tmux session — no further emux mediation.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .server import (
    _live_sessions,
    _load_registry,
    _resolve_tmux,
    _save_registry,
    converse,
    run_mcp_server,
)


def _attach_to_session(session: str) -> None:
    """Replace this process with `tmux attach -t <session>`. Does not return."""
    tmux = _resolve_tmux()
    if tmux is None:
        print("emux: tmux not on PATH. install with `brew install tmux` or equivalent.", file=sys.stderr)
        sys.exit(2)
    os.execv(tmux, [tmux, "attach", "-t", session])


def _interactive_register(default_name: str | None = None) -> tuple[str, str] | None:
    """Prompt for a new registry entry. Returns (name, session) or None on abort."""
    print()
    name = input("  registry name (e.g. 'claude-prod'): ").strip()
    if not name:
        print("  aborted.")
        return None
    session_default = f" [{default_name}]" if default_name else ""
    session = input(f"  tmux session id{session_default}: ").strip() or (default_name or "")
    if not session:
        print("  aborted (no session id).")
        return None
    description = input("  description (optional): ").strip() or None
    tags_in = input("  tags (space-separated, optional): ").strip()
    tags = tags_in.split() if tags_in else []

    import time

    registry = _load_registry()
    registry[name] = {
        "session": session,
        "description": description,
        "tags": tags,
        "registered_at": int(time.time()),
    }
    _save_registry(registry)
    print(f"\n  registered '{name}' → {session}.")
    return name, session


def cmd_picker() -> int:
    """Run the textual TUI picker, then dispatch the user's selection."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        print("       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).", file=sys.stderr)
        return 2

    from .tui import run_tui

    result = run_tui()
    if result is None:
        # User quit, or there was nothing to pick.
        return 0

    action = result["action"]
    if action == "attach":
        _attach_to_session(result["session"])
        return 0  # not reached; execv replaces us
    if action == "register_then_attach":
        reg = _interactive_register(default_name=result["default_session"])
        if reg is None:
            return 0
        _attach_to_session(reg[1])
        return 0
    if action == "register_new":
        reg = _interactive_register()
        if reg is None:
            return 0
        prompt = f"\n  attach to '{reg[1]}' now? [Y/n]: "
        attach = input(prompt).strip().lower()
        if attach in {"", "y"}:
            _attach_to_session(reg[1])
        return 0
    if action == "unregister":
        registry = _load_registry()
        if result["name"] in registry:
            removed = registry.pop(result["name"])
            _save_registry(registry)
            print(f"\n  unregistered '{result['name']}' (was → {removed['session']}).")
        return 0

    print(f"emux: unknown TUI result action: {action!r}", file=sys.stderr)
    return 1


def cmd_ls() -> int:
    """Print registered + live sessions to stdout. Non-interactive; CI-friendly."""
    registry = _load_registry()
    live = _live_sessions()
    live_names = {s["name"] for s in live}

    print("registered sessions:")
    if not registry:
        print("  (none)")
    else:
        for name, entry in sorted(registry.items()):
            stale = " STALE" if entry["session"] not in live_names else ""
            desc = f" — {entry['description']}" if entry.get("description") else ""
            print(f"  {name} → {entry['session']}{stale}{desc}")

    print("\nlive tmux sessions:")
    if not live:
        print("  (none — `tmux list-sessions` returned no sessions)")
    else:
        registered_sessions = {entry["session"] for entry in registry.values()}
        for s in live:
            mark = " (registered)" if s["name"] in registered_sessions else ""
            attached = " (attached)" if s.get("attached") else ""
            print(f"  {s['name']}{mark}{attached}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Send a prompt to an AI running in a tmux session, print its reply.

    The session must already be running the AI (emux never spawns) and sitting
    at its input prompt. Waits until the pane stops changing before reading.
    """
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        return 2
    result = converse(
        target=args.target,
        prompt=args.prompt,
        settle_seconds=args.settle,
        max_seconds=args.max,
        by_registry_name=args.by_name,
        busy_markers=args.busy or None,
    )
    if not result.get("ok"):
        print(f"emux ask: {result.get('error', 'failed')}", file=sys.stderr)
        return 1
    if args.screen:
        print(result["screen"])
    else:
        print(result["reply"] or "(no new output — the AI may still be responding)")
    if not result["settled"]:
        print(f"\n[emux: hit {args.max:g}s cap; reply may be truncated]", file=sys.stderr)
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    """Non-interactive register command for scripting."""
    import time
    registry = _load_registry()
    registry[args.name] = {
        "session": args.session,
        "description": args.description,
        "tags": args.tags or [],
        "manages": args.manages or [],
        "registered_at": int(time.time()),
    }
    _save_registry(registry)
    print(f"registered '{args.name}' → {args.session}")
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    registry = _load_registry()
    if args.name not in registry:
        print(f"emux: '{args.name}' not registered.", file=sys.stderr)
        return 1
    removed = registry.pop(args.name)
    _save_registry(registry)
    print(f"unregistered '{args.name}' (was → {removed['session']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emux",
        description="Eidos mux — pick up where you left off in tmux. TUI picker by default; subcommands for scripting and the MCP server.",
    )
    parser.add_argument("--version", action="version", version=f"emux {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("mcp", help="start the emux MCP server (stdio)")
    sub.add_parser("ls", help="print registered + live sessions (non-interactive)")

    p_ask = sub.add_parser("ask", help="send a prompt to an AI in a tmux session, wait for it to settle, print the reply")
    p_ask.add_argument("target", help="tmux session name (or registry name with -n)")
    p_ask.add_argument("prompt", help="the message to send to the AI")
    p_ask.add_argument("-n", "--by-name", action="store_true", help="resolve target via the registry")
    p_ask.add_argument("--settle", type=float, default=2.5, help="reply is done once the pane is unchanged this long (default 2.5s)")
    p_ask.add_argument("--max", type=float, default=90.0, help="hard cap on total wait (default 90s)")
    p_ask.add_argument("-b", "--busy", action="append", metavar="MARKER",
                       help="substring meaning the AI is still working (e.g. 'thinking'); never settle while on screen. Repeatable.")
    p_ask.add_argument("--screen", action="store_true", help="print the full settled pane instead of just the reply delta")

    p_web = sub.add_parser("web", help="start the web daemon — monitor sessions in a browser (grid/groups/activity/flow/chat)")
    p_web.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; no auth — keep it local)")
    p_web.add_argument("--port", type=int, default=8689, help="port (default 8689)")
    p_web.add_argument("--open", action="store_true", help="open the browser after starting")
    p_web.add_argument("--print-launchd", action="store_true",
                       help="print a launchd plist that keeps the daemon running, then exit")

    p_reg = sub.add_parser("register", help="register a session under a friendly name")
    p_reg.add_argument("name")
    p_reg.add_argument("session")
    p_reg.add_argument("-d", "--description", default=None)
    p_reg.add_argument("-t", "--tags", nargs="*")
    p_reg.add_argument("-m", "--manages", nargs="*",
                       help="other registered names (or session ids) this agent manages — drawn as arrows in `emux web` flow view")

    p_unreg = sub.add_parser("unregister", help="remove a session from the registry")
    p_unreg.add_argument("name")

    args = parser.parse_args(argv)

    if args.cmd is None:
        # Bare `emux` → TUI picker.
        return cmd_picker()
    if args.cmd == "mcp":
        run_mcp_server()
        return 0
    if args.cmd == "web":
        if args.print_launchd:
            from .web import launchd_plist
            print(launchd_plist(host=args.host, port=args.port), end="")
            return 0
        from .web import run_web
        return run_web(host=args.host, port=args.port, open_browser=args.open)
    if args.cmd == "ls":
        return cmd_ls()
    if args.cmd == "ask":
        return cmd_ask(args)
    if args.cmd == "register":
        return cmd_register(args)
    if args.cmd == "unregister":
        return cmd_unregister(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
