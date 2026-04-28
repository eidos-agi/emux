"""emux CLI dispatcher.

  emux              → TUI picker (registered + live tmux sessions)
  emux mcp          → start the MCP server
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
from typing import Any

from . import __version__
from .server import (
    _live_sessions,
    _load_registry,
    _resolve_tmux,
    _save_registry,
    run_mcp_server,
)


def _print_table(rows: list[tuple[str, str, str]]) -> None:
    """Print a 3-column aligned table: # · NAME · DETAIL."""
    if not rows:
        return
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    for r in rows:
        print(f"  {r[0]:>{w0}}  {r[1]:<{w1}}  {r[2]}")


def _build_picker_choices() -> list[dict[str, Any]]:
    """Build the ordered list of choices for the TUI picker.

    Each choice: {label, kind: 'registered'|'live'|'register-new', session?, name?, detail}.
    """
    registry = _load_registry()
    live = _live_sessions()
    live_names = {s["name"] for s in live}

    choices: list[dict[str, Any]] = []

    # 1. Registered sessions, ordered by registration time (newest first).
    for name, entry in sorted(
        registry.items(),
        key=lambda kv: -int(kv[1].get("registered_at", 0)),
    ):
        session = entry["session"]
        is_stale = session not in live_names
        desc = entry.get("description") or ""
        tags = entry.get("tags") or []
        tag_str = " ".join(f"#{t}" for t in tags)
        status = "STALE — tmux session gone" if is_stale else "live"
        detail_parts = [f"→ {session}", status]
        if desc:
            detail_parts.append(f"— {desc}")
        if tag_str:
            detail_parts.append(tag_str)
        choices.append({
            "kind": "registered",
            "name": name,
            "session": session,
            "is_stale": is_stale,
            "detail": "  ".join(detail_parts),
        })

    # 2. Live sessions not yet in the registry.
    registered_sessions = {entry["session"] for entry in registry.values()}
    for s in live:
        if s["name"] not in registered_sessions:
            attached = " (attached)" if s.get("attached") else ""
            choices.append({
                "kind": "live",
                "session": s["name"],
                "detail": f"unregistered live tmux session{attached}",
            })

    # 3. Always-available action: register a new entry by hand.
    choices.append({
        "kind": "register-new",
        "detail": "register a new session by typing name + tmux session id",
    })

    return choices


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
    """Run the interactive TUI picker. Returns process exit code."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        print("       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).", file=sys.stderr)
        return 2

    choices = _build_picker_choices()
    print(f"\nemux v{__version__} — pick a session to attach\n")

    rows: list[tuple[str, str, str]] = []
    for i, c in enumerate(choices, 1):
        if c["kind"] == "registered":
            label = c["name"]
        elif c["kind"] == "live":
            label = c["session"]
        else:  # register-new
            label = "(register new)"
        rows.append((str(i), label, c["detail"]))
    _print_table(rows)
    print()

    raw = input("  pick [1-{}], or q to quit: ".format(len(choices))).strip()
    if raw.lower() in {"q", "quit", "exit", ""}:
        return 0
    try:
        idx = int(raw) - 1
    except ValueError:
        print(f"  invalid selection: {raw!r}")
        return 1
    if not 0 <= idx < len(choices):
        print(f"  out of range: {raw}")
        return 1

    chosen = choices[idx]
    if chosen["kind"] == "registered":
        if chosen["is_stale"]:
            print(f"\n  '{chosen['name']}' is stale (tmux session '{chosen['session']}' gone).")
            print("  options: re-register against a live session, unregister, or pick again.")
            return 1
        print(f"\n  attaching: {chosen['name']} → {chosen['session']}")
        _attach_to_session(chosen["session"])
        return 0  # not reached; execv replaces us
    elif chosen["kind"] == "live":
        # Offer to register on the fly, then attach.
        print(f"\n  '{chosen['session']}' is live but not registered.")
        ans = input("  register it now? [y/N]: ").strip().lower()
        if ans == "y":
            result = _interactive_register(default_name=chosen["session"])
            if result is None:
                return 0
        print(f"\n  attaching: {chosen['session']}")
        _attach_to_session(chosen["session"])
        return 0
    else:  # register-new
        result = _interactive_register()
        if result is None:
            return 0
        _name, session = result
        attach = input(f"\n  attach to '{session}' now? [Y/n]: ").strip().lower()
        if attach in {"", "y"}:
            _attach_to_session(session)
        return 0


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


def cmd_register(args: argparse.Namespace) -> int:
    """Non-interactive register command for scripting."""
    import time
    registry = _load_registry()
    registry[args.name] = {
        "session": args.session,
        "description": args.description,
        "tags": args.tags or [],
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

    p_reg = sub.add_parser("register", help="register a session under a friendly name")
    p_reg.add_argument("name")
    p_reg.add_argument("session")
    p_reg.add_argument("-d", "--description", default=None)
    p_reg.add_argument("-t", "--tags", nargs="*")

    p_unreg = sub.add_parser("unregister", help="remove a session from the registry")
    p_unreg.add_argument("name")

    args = parser.parse_args(argv)

    if args.cmd is None:
        # Bare `emux` → TUI picker.
        return cmd_picker()
    if args.cmd == "mcp":
        run_mcp_server()
        return 0
    if args.cmd == "ls":
        return cmd_ls()
    if args.cmd == "register":
        return cmd_register(args)
    if args.cmd == "unregister":
        return cmd_unregister(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
